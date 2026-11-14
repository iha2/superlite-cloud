"""Worker: claims tasks and runs an agent CLI in a sandbox."""

from __future__ import annotations

import dataclasses
import os
import shutil
from dataclasses import dataclass
from typing import Any, Callable

from slcloud import gitops
from slcloud.adapters import get_adapter
from slcloud.sandbox import Job
from slcloud.vault import mask

NOTICE = "Run `superlite-cli orchestrator set ai` first."
PLAN_MARK = "Reply approve to start"


@dataclass
class Deps:
    engine: Any
    vault: Any
    runner: Any
    settings: Any
    clone: Callable = gitops.clone_at
    hand_back: Callable = gitops.hand_back


def _fail(task, deps: Deps, message: str, secrets: list[str] | None = None):
    text = mask(message, secrets or [])
    return deps.engine.transition(task.id, "failed", error=text[-2000:])


def run_task(task, deps: Deps):
    workdir = os.path.join(deps.settings.work_dir, task.id)
    keep = False
    try:
        res = _run_task(task, deps, workdir)
        keep = getattr(res, "status", None) == "waiting"
        return res
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def _run_task(task, deps: Deps, workdir: str):
    eng = deps.engine
    secrets: list[str] = []
    try:
        adapter = get_adapter(task.agent)
        key = deps.vault.get(task.ws, adapter.provider)
        if not key:
            return _fail(task, deps, f"no {adapter.provider} key")
        secrets = [key]
        st = deps.settings
        resuming = bool(task.session and task.answer)
        if resuming:
            prompt, session = task.answer, task.session
            if not adapter.supports_resume:
                session = None
        else:
            deps.clone(task.repo, task.base, workdir)
            prompt, session = task.prompt, None
            if not adapter.supports_resume:
                prompt = f"{NOTICE} {prompt}"
                eng.add_event(task.id, "notice", {"text": NOTICE})
        job = Job(st.worker_image, adapter.command(prompt, session),
                  {adapter.key_env: key}, workdir)
        res = deps.runner.run(job)
        if res.timed_out:
            return _fail(task, deps, "timed out")
        if res.code != 0:
            return _fail(task, deps, res.stderr or res.stdout or f"exit {res.code}", secrets)
        parsed = adapter.parse(res.stdout)
        if not parsed.ok:
            return _fail(task, deps, parsed.text or "agent failed", secrets)
        if parsed.plan or PLAN_MARK in (parsed.text or ""):
            return eng.transition(task.id, "waiting", session=parsed.session,
                                  plan=mask(parsed.plan or parsed.text, secrets))
        hb = deps.hand_back(workdir, f"slcloud/{task.id}",
                            os.path.join(st.data_dir, "bundles"), True)
        result = {k: mask(v, secrets) if isinstance(v, str) else v
                  for k, v in dataclasses.asdict(hb).items()}
        return eng.transition(task.id, "done", result=result)
    except Exception as exc:  # noqa: BLE001 - any failure fails the task, masked
        return _fail(task, deps, f"{type(exc).__name__}: {exc}", secrets)


def loop_once(deps: Deps) -> bool:
    task = deps.engine.claim()
    if task is None:
        return False
    try:
        run_task(task, deps)
    except Exception:  # noqa: BLE001 - e.g. task cancelled mid-run; never kill the loop
        pass
    return True
