"""Worker: runs one long-lived agent per run in a sandbox, streams events, takes nudges,
and hands the branch back through the shared repo cache."""

from __future__ import annotations

import dataclasses
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from slcloud import gitops, names, repocache
from slcloud.adapters import PLAN_MARKER, get_adapter
from slcloud.capture import Capture
from slcloud.drivers import get_driver
from slcloud.sandbox import Job, Mount
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


def _fail(task, deps: Deps, message: str, secrets: list[str] | None = None):
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


def _pump_nudges(task, deps: Deps, driver, stop: threading.Event, secrets: list[str]) -> None:
    eng, off = deps.engine, False
    while not stop.is_set():
        try:
            for n in eng.pending_nudges(task.id):
                if off:
                    eng.mark_nudge(n["id"], "unsupported")
                    continue
                try:
                    res = driver.nudge(n["text"])
                except Exception:  # noqa: BLE001 - a broken driver means watch-only
                    res = "unsupported"
                if res == "delivered":
                    eng.mark_nudge(n["id"], "delivered")
                    eng.add_event(task.id, "nudge",
                                  {"text": f"nudge from {_handle(deps, n['sender'])}"})
                else:
                    off = True
                    eng.mark_nudge(n["id"], "unsupported")
                    eng.add_event(task.id, "notice", {"text": "nudges are off"})
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
        sb = _Sandbox(task, deps, run_dir, cache, {adapter.key_env: key})
        if not task.base or task.base.startswith("-") or any(c.isspace() or ord(c) < 32 for c in task.base):
            return _fail(task, deps, "invalid base")  # never let a base act as a git option
        state = {"name": task.name}
        resuming = bool(task.session and task.answer) and os.path.isdir(run_dir)
        if resuming:
            prompt, session = task.answer, task.session
        else:
            prompt, session = task.prompt, None
            shutil.rmtree(run_dir, ignore_errors=True)
            os.makedirs(os.path.dirname(run_dir), exist_ok=True)
            with cache.lock():
                cache.fetch()
                cache.light_clone(task.base, run_dir)
            sb.sh([*deps.cli, "session", "create", "--slug", names.slug(task.name),
                   "--base", task.base, "--branch-name", "superlite"])

        cap = Capture(task.id, eng.add_event, secrets, deps.clock)
        driver = get_driver(task.agent, sb.start, session)
        driver.start(prompt)
        stop = threading.Event()
        pump = threading.Thread(target=_pump_nudges, args=(task, deps, driver, stop, secrets),
                                daemon=True)
        pump.start()
        end = None
        try:
            for line in driver.lines():
                for ev in driver.events(line):
                    end = _turn_end(ev) or end
                    if _turn_end(ev) is None:
                        cap.feed(ev)
                if end:
                    break
        finally:
            stop.set()
            pump.join(5)
            cap.flush()
            driver.stop()
        proc = getattr(driver, "proc", None)
        r = proc.wait() if proc is not None else None
        if r is not None and r.timed_out:
            return _fail(task, deps, "timed out")
        res = driver.result()
        if end is None:
            err = (r.stderr if r is not None else "") or res.text or f"exit {getattr(r, 'code', '?')}"
            return _fail(task, deps, err, secrets)
        if end == "error" or not res.ok:
            return _fail(task, deps, res.text or "agent failed", secrets)
        if res.plan or PLAN_MARK in (res.text or ""):
            return eng.transition(task.id, "waiting", session=driver.session or res.session,
                                  plan=mask(res.plan or res.text, secrets))

        def rename(old: str, new: str) -> None:
            sb.sh([*deps.cli, "session", "rename", "--slug", names.slug(old), "--to", names.slug(new)])
            eng.rename(task.id, new)
            state["name"] = new

        def commit() -> None:
            sb.sh(["sh", "-c", _COMMIT], {**_GIT_ENV, "SL_MSG": f"superlite: {state['name']}"})

        def log(msg: str) -> None:
            eng.add_event(task.id, "notice", {"text": msg})

        hb = gitops.safe_hand_back(cache, run_dir, task.name, task.id, set(), rename, commit, log,
                                   os.path.join(st.data_dir, "bundles"))
        eng.add_event(task.id, "handback", {"text": f"handed back on {hb.branch}"})
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
    task = deps.engine.claim()
    if task is None:
        return False
    try:
        run_task(task, deps)
    except Exception:  # noqa: BLE001 - e.g. task cancelled mid-run; never kill the loop
        pass
    _cleanup(deps)
    return True
