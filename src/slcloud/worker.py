"""Worker: runs one long-lived agent per run in a sandbox, streams events, takes nudges,
and hands the branch back through the shared repo cache."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from slcloud import files, filestore, gitops, names, repocache
from slcloud.adapters import PLAN_MARKER, get_adapter, is_question, redact
from slcloud.capture import Capture
from slcloud.drivers import get_driver
from slcloud.sandbox import Job, Mount, remove_state
from slcloud.vault import mask

PLAN_MARK = PLAN_MARKER
_TERMINAL = ("done", "failed", "cancelled")
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "superlite",
            "GIT_AUTHOR_EMAIL": "superlite@localhost", "GIT_COMMITTER_NAME": "superlite",
            "GIT_COMMITTER_EMAIL": "superlite@localhost"}
_SECRET_GLOBS = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
                 ".npmrc", ".netrc", ".pypirc", "credentials*.json")
_COMMIT = ("git add -A -- . " + " ".join(f"':(exclude,glob)**/{g}'" for g in _SECRET_GLOBS) + " && "
           "(git diff --cached --quiet || git commit --quiet -m \"$SL_MSG\")")


@dataclass
class Deps:
    engine: Any
    vault: Any
    runner: Any
    settings: Any
    clock: Callable[[], float] = time.time
    poll: float = 0.5
    cli: list[str] = field(default_factory=lambda: [
        "bash", "/opt/superlite/skills/swarm/scripts/superlite-cli"])


def answer_for(answer: str, edited: list[str]) -> str:
    if not edited:
        return answer
    return (f"{answer}\n\nBefore approving, the user edited these files on the branch: "
            f"{', '.join(edited)}. Reread them before you build.")


def _is_plan(path: str) -> bool:
    return path.startswith("docs/plans/") and path.endswith(".md")


def _base_sha(cache, base: str, gate: str) -> str:
    p = subprocess.run(["git", "-C", cache.path, "merge-base", base, gate],
                       capture_output=True, text=True, timeout=60)
    return p.stdout.strip() if p.returncode == 0 else ""


def _pub_ref(task_id: str) -> str:
    return f"refs/published/{task_id}"


def _published(cache, task_id: str) -> str:
    """The sha an earlier publish pushed for this task (survives the row clear on resume)."""
    p = subprocess.run(["git", "-C", cache.path, "rev-parse", "--verify", "-q", _pub_ref(task_id)],
                       capture_output=True, text=True, timeout=60)
    return p.stdout.strip() if p.returncode == 0 else ""


def _has_run_refs(cache, task_id: str) -> bool:
    p = subprocess.run(["git", "-C", cache.path, "for-each-ref", "--count=1",
                        f"refs/runs/{task_id}/"], capture_output=True, text=True, timeout=60)
    return p.returncode == 0 and bool(p.stdout.strip())


def _gate(task, deps: Deps, cache, sb, branch: str) -> None:
    """Commit the checkout, copy it into the cache as the gate ref and record the plans."""
    sb.sh(["sh", "-c", _COMMIT], {**_GIT_ENV, "SL_MSG": "superlite: plan gate"})
    with cache.lock():
        sha = cache.fetch_from(sb.run_dir, branch, into=files.gate_ref(task.id))
        base = _base_sha(cache, task.base, sha)
        names_ = files.changed(cache, base, sha) if base else []
    plans = [p for p in names_ if _is_plan(p)]
    filestore.set_gate_sha(deps.engine.store, task.id, sha, plans)
    deps.engine.add_live_event(task.id, "plan", {"plans": plans})


def publish_pending(deps: Deps) -> bool:
    """Serve publish requests: push each gate to origin, else keep a bundle."""
    eng, st = deps.engine, deps.settings
    done = False
    for tid in filestore.pending(eng.store):
        try:
            task = eng._load(tid)
            cache = repocache.cache_for(st.data_dir, task.ws, task.repo)
            if task.status != "waiting":
                # Cancelled, finished or approved since the request: never push a stale gate.
                if task.status in _TERMINAL:
                    with cache.lock():
                        files.drop_refs(cache, tid)
                        filestore.clear(eng.store, tid)
                else:
                    filestore.mark_gated(eng.store, tid)
                continue
            row = filestore.get(eng.store, tid) or {}
            branch = "superlite/sessions/" + names.slug(task.name)
            gref, bundle = files.gate_ref(tid), ""
            with cache.lock():
                filestore.require_waiting(eng.store, tid)
                sha = row.get("sha") or cache.fetch_from(
                    os.path.join(st.work_dir, tid), branch, into=gref)
                pushed = cache.push(gref, branch, expect=_published(cache, tid))
                if pushed:
                    subprocess.run(["git", "-C", cache.path, "update-ref", _pub_ref(tid), sha],
                                   capture_output=True, timeout=60, check=True)
                if not pushed:
                    bundle = cache.bundle(gref, os.path.join(st.data_dir, "bundles", f"{tid}.gate.bundle"))
                filestore.set_gate(eng.store, tid, branch, sha, pushed, row.get("plans") or [], bundle)
            eng.add_live_event(tid, "published", {"branch": branch, "sha": sha, "pushed": pushed})
            done = True
        except Exception:  # noqa: BLE001 - one bad request must not stop the loop
            continue
    return done


def _cancelled(task, deps: Deps) -> bool:
    try:
        return deps.engine.is_cancelled(task.id)
    except Exception:  # noqa: BLE001 - an unreadable status is not a cancel
        return False


def _gone(task, deps: Deps):
    """The cancelled task, as left by the user; the worker changes and emits nothing more."""
    return deps.engine._load(task.id)


def _fail(task, deps: Deps, message: str, secrets: list[str] | None = None):
    if _cancelled(task, deps):
        return _gone(task, deps)
    text = mask(message, secrets or [])
    return deps.engine.transition(task.id, "failed", error=text[-2000:])


def run_task(task, deps: Deps):
    run_dir = os.path.join(deps.settings.work_dir, task.id)
    keep = False
    try:
        res = _run_task(task, deps, run_dir)
        keep = getattr(res, "status", None) == "waiting"
        return res
    finally:
        if not keep:
            shutil.rmtree(run_dir, ignore_errors=True)
            remove_state(run_dir)


def _turn_end(ev: dict) -> str | None:
    """Normalise both turn-end shapes to 'done' or 'error' (else None)."""
    kind = ev.get("kind")
    if kind == "status":
        return ev.get("status") if ev.get("status") in ("done", "error") else None
    return kind if kind in ("done", "error") else None


class _Sandbox:
    def __init__(self, task, deps: Deps, run_dir: str, cache, env: dict[str, str]):
        self.deps, self.run_dir, self.env = deps, run_dir, env
        self.mounts = [Mount(cache.path, cache.path, True)]

    @property
    def workdir(self) -> str:
        """Where the agent sees the checkout: /work in a container, the run dir under `process`."""
        for_ = getattr(self.deps.runner, "workdir_for", None)
        return for_(self.run_dir) if for_ else "/work"

    def job(self, command: list[str], env: dict[str, str] | None = None) -> Job:
        return Job(self.deps.settings.worker_image, command, {**self.env, **(env or {})},
                   self.run_dir, mounts=list(self.mounts))

    def start(self, command: list[str]):
        return self.deps.runner.start(self.job(command))

    def sh(self, command: list[str], env: dict[str, str] | None = None) -> None:
        p = self.deps.runner.start(self.job(command, env))
        p.close_stdin()
        for _ in p.lines():
            pass
        r = p.wait()
        if r.timed_out or r.code != 0:
            raise RuntimeError((r.stderr or f"exit {r.code}").strip()[-500:])


def _handle(deps: Deps, member: str) -> str:
    try:
        rows = deps.engine.store.query("SELECT handle FROM members WHERE id=?", (member,))
    except Exception:  # noqa: BLE001 - label only
        rows = []
    return rows[0]["handle"] if rows else member


def _kill(driver) -> None:
    """Stop the agent now: end its input, then kill the sandbox job (container removed too)."""
    try:
        driver.stop()
    finally:
        proc = getattr(driver, "proc", None)
        if proc is not None:
            proc.kill()


def _pump_nudges(task, deps: Deps, driver, stop: threading.Event, secrets: list[str],
                 cancel: threading.Event | None = None) -> None:
    eng, off = deps.engine, False
    cancel = cancel if cancel is not None else threading.Event()
    # Nudges a driver has queued for its next turn: still pending in the inbox until that turn starts.
    lock, accepted = threading.Lock(), []

    def resumed(count: int) -> None:
        with lock:
            batch, accepted[:] = accepted[:count], accepted[count:]
        for n in batch:
            try:
                eng.mark_nudge(n["id"], "delivered")
                eng.add_live_event(task.id, "nudge", {"by": _handle(deps, n["sender"]),
                                                      "text": redact(mask(n["text"], secrets))})
            except Exception:  # noqa: BLE001 - bookkeeping must not break the turn
                pass

    if hasattr(driver, "on_resume"):
        driver.on_resume = resumed
    while not stop.is_set():
        try:
            if _cancelled(task, deps):
                cancel.set()
                _kill(driver)
                return
            for n in eng.pending_nudges(task.id):
                if off:
                    eng.mark_nudge(n["id"], "unsupported")
                    continue
                with lock:
                    if any(a["id"] == n["id"] for a in accepted):
                        continue  # already queued in the driver; waits for the resumed turn
                    try:
                        res = driver.nudge(n["text"])
                    except Exception:  # noqa: BLE001 - a broken driver means watch-only
                        res = "unsupported"
                    if res == "queued":
                        accepted.append(n)
                        continue
                if res == "delivered":
                    eng.mark_nudge(n["id"], "delivered")
                    eng.add_live_event(task.id, "nudge",
                                       {"by": _handle(deps, n["sender"]),
                                        "text": redact(mask(n["text"], secrets))})
                else:
                    off = True
                    eng.mark_nudge(n["id"], "unsupported")
                    eng.add_live_event(task.id, "notice", {"text": "nudges are off"})
        except Exception:  # noqa: BLE001 - polling must never kill the run
            pass
        stop.wait(deps.poll)


def _run_task(task, deps: Deps, run_dir: str):
    eng, st = deps.engine, deps.settings
    secrets: list[str] = []
    try:
        adapter = get_adapter(task.agent)
        key = deps.vault.get(task.ws, adapter.provider)
        if not key:
            return _fail(task, deps, f"no {adapter.provider} key")
        secrets = [key]
        cache = repocache.cache_for(st.data_dir, task.ws, task.repo)
        sb = _Sandbox(task, deps, run_dir, cache, {**adapter.env, adapter.key_env: key})
        if not task.base or task.base.startswith("-") or any(c.isspace() or ord(c) < 32 for c in task.base):
            return _fail(task, deps, "invalid base")  # never let a base act as a git option
        state = {"name": task.name}
        resuming = bool(task.session and task.answer) and os.path.isdir(run_dir)
        pushed_gate = bool(_published(cache, task.id)) if resuming else False
        if resuming:
            prompt, session = task.answer, task.session
            with cache.lock():
                row = filestore.get(eng.store, task.id)
                if row:
                    pushed_gate = pushed_gate or bool(row["pushed"])
                    edited: list[str] = []
                    if row["edits"]:
                        sha = row["edits"]["sha"]
                        sb.sh(["git", "fetch", "-q", cache.path, files.edits_ref(task.id)])
                        sb.sh(["git", "merge", "--ff-only", "-q", sha])
                        edited = list(row["edits"]["files"])
                    prompt = answer_for(task.answer, edited)
                    files.drop_refs(cache, task.id)
                    filestore.clear(eng.store, task.id)
        else:
            prompt, session = task.prompt, None
            shutil.rmtree(run_dir, ignore_errors=True)
            remove_state(run_dir)
            os.makedirs(os.path.dirname(run_dir), exist_ok=True)
            with cache.lock():
                lost = filestore.get(eng.store, task.id)
                if lost is not None or _has_run_refs(cache, task.id):
                    files.drop_refs(cache, task.id)
                    filestore.clear(eng.store, task.id)
                    if lost and lost["edits"]:
                        eng.add_live_event(task.id, "notice", {
                            "text": "the checkout was gone, so the edits you pushed were not applied"})
                cache.fetch()
                cache.light_clone(task.base, run_dir)
            sb.sh([*deps.cli, "session", "create", "--slug", names.slug(task.name),
                   "--base", task.base, "--branch-name", "superlite"])

        if _cancelled(task, deps):
            return _gone(task, deps)
        cap = Capture(task.id, eng.add_live_event, secrets, deps.clock)
        driver = get_driver(task.agent, sb.start, session, slug=names.slug(task.name),
                            workdir=sb.workdir)
        driver.orchestrator = getattr(task, "orchestrator", None) or "ai"
        try:
            driver.start(prompt)
        except BaseException:
            _kill(driver)  # a failed handshake must not leave the sandbox running
            raise
        stop, cancel = threading.Event(), threading.Event()
        pump = threading.Thread(target=_pump_nudges, args=(task, deps, driver, stop, secrets, cancel),
                                daemon=True)
        pump.start()
        end = None
        try:
            for line in driver.lines():
                for ev in driver.events(line):
                    end = _turn_end(ev) or end
                    if _turn_end(ev) is None:
                        cap.feed(ev)
                if end or cancel.is_set():
                    break
        finally:
            stop.set()
            pump.join(5)
            if _cancelled(task, deps):
                cancel.set()
                _kill(driver)
            else:
                cap.flush()
                driver.stop()
        proc = getattr(driver, "proc", None)
        r = proc.wait() if proc is not None else None
        if cancel.is_set() or _cancelled(task, deps):
            return _gone(task, deps)
        if r is not None and r.timed_out:
            return _fail(task, deps, "timed out")
        res = driver.result()
        if end is None:
            err = (r.stderr if r is not None else "") or res.text or f"exit {getattr(r, 'code', '?')}"
            return _fail(task, deps, err, secrets)
        if end == "error" or not res.ok:
            return _fail(task, deps, res.text or "agent failed", secrets)
        final = mask(res.text or res.plan or "", secrets)
        if res.plan or PLAN_MARK in (res.text or ""):
            eng.add_live_event(task.id, "result", {"text": final})
            _gate(task, deps, cache, sb, "superlite/sessions/" + names.slug(state["name"]))
            return eng.transition(task.id, "waiting", session=driver.session or res.session,
                                  plan=mask(res.plan or res.text, secrets), waiting_on="plan")
        if driver.orchestrator == "human" and is_question(res.text or ""):
            lines = (res.text or "").strip().splitlines()
            ask = mask("\n".join(lines[:-1]).strip(), secrets)  # text minus the marker line
            eng.add_live_event(task.id, "question", {"run": task.name, "text": ask})
            eng.add_live_event(task.id, "result", {"text": final})
            return eng.transition(task.id, "waiting", session=driver.session or res.session,
                                  waiting_on="question")

        def rename(old: str, new: str) -> str:
            new = eng.rename(task.id, new).name  # reserved against other live runs; may differ
            sb.sh([*deps.cli, "session", "rename", "--slug", names.slug(old), "--to", names.slug(new)])
            state["name"] = new
            eng.add_live_event(task.id, "rename", {"from": old, "to": new, "why": "branch taken on origin"})
            return new

        def commit() -> None:
            sb.sh(["sh", "-c", _COMMIT], {**_GIT_ENV, "SL_MSG": f"superlite: {state['name']}"})

        def log(msg: str) -> None:
            eng.add_live_event(task.id, "notice", {"text": msg})

        if _cancelled(task, deps):
            return _gone(task, deps)
        hb = gitops.safe_hand_back(cache, run_dir, task.name, task.id,
                                   {task.name} if pushed_gate else set(), rename, commit, log,
                                   os.path.join(st.data_dir, "bundles"),
                                   live=lambda: eng.live_names(task.id))
        eng.add_live_event(task.id, "handback", {"text": f"handed back on {hb.branch}"})
        eng.add_live_event(task.id, "result", {"text": final})
        result = {k: mask(v, secrets) if isinstance(v, str) else v
                  for k, v in dataclasses.asdict(hb).items()}
        return eng.transition(task.id, "done", result=result)
    except Exception as exc:  # noqa: BLE001 - any failure fails the task, masked
        return _fail(task, deps, f"{type(exc).__name__}: {exc}", secrets)


def _cleanup(deps: Deps) -> None:
    try:
        st, eng = deps.settings, deps.engine
        rows = eng.store.query(
            "SELECT workspace, repo FROM tasks WHERE status NOT IN ('done','failed','cancelled')")
        in_use = {repocache.cache_for(st.data_dir, r["workspace"], r["repo"]).path for r in rows}
        repocache.cleanup(st.data_dir, in_use, deps.clock())
    except Exception:  # noqa: BLE001 - housekeeping only
        pass


def loop_once(deps: Deps) -> bool:
    try:
        if publish_pending(deps):
            return True
    except Exception:  # noqa: BLE001 - never kill the loop
        pass
    task = deps.engine.claim()
    if task is None:
        return False
    try:
        run_task(task, deps)
    except Exception:  # noqa: BLE001 - e.g. task cancelled mid-run; never kill the loop
        pass
    _cleanup(deps)
    return True
