"""Live event stream (SSE) and nudge routes for a run."""

from __future__ import annotations

import json
import time
from typing import Callable, Iterator

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .errors import NotFound


def _get_run(store, who, ref):
    from . import runs

    return runs.get_run(store, who, ref)


def _seen(store, who, task) -> None:
    from . import runs

    runs.mark_seen_run(store, who.member, task.id)


TERMINAL = ("done", "failed", "cancelled")


def sse_lines(engine, task_id: str, after: int, alive: Callable[[], bool],
              sleep: Callable[[float], None], keepalive_s: float = 15.0,
              clock: Callable[[], float] = time.monotonic,
              run: str | None = None,
              finished: Callable[[], bool] | None = None) -> Iterator[str]:
    """Yield SSE frames for events after `after` until alive() is False, or until the run is
    terminal (done, failed, cancelled) and its remaining events are flushed."""
    last = after
    quiet_since = clock()
    ending = False
    while alive():
        term = ending or (finished is not None and finished())  # read before the fetch
        fresh = [e for e in engine.events(task_id, after=last) if e["id"] > last]
        if term and not fresh:
            return
        if fresh:
            for e in fresh:
                if e["kind"] == "status" and (e.get("data") or {}).get("to") in TERMINAL:
                    ending = True
                data = dict(e.get("data") or {})
                if run is not None:
                    data = {"run": run, **data}
                last = e["id"]
                yield f"id: {e['id']}\nevent: {e['kind']}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
            quiet_since = clock()
        elif clock() - quiet_since >= keepalive_s:
            yield ": keep-alive\n\n"
            quiet_since = clock()
        if not alive():
            return
        if not ending:
            sleep(1.0)


def make_router(store, engine, whoami: Callable) -> APIRouter:
    router = APIRouter()

    def nf() -> JSONResponse:
        return JSONResponse({"error": "not found"}, status_code=404)

    @router.get("/api/runs/{ref:path}/events")
    def events(ref: str, request: Request, after: int = 0):
        who = whoami()
        try:
            task = _get_run(store, who, ref)
        except NotFound:
            return nf()
        _seen(store, who, task)
        ref = task.id  # pin: a later rename or give must not retarget the access and alive checks
        hdr = request.headers.get("last-event-id", "")
        if hdr.isdigit():
            after = int(hdr)

        def alive() -> bool:
            rows = store.query("SELECT locked FROM members WHERE id=?", (who.member,))
            if not rows or rows[0]["locked"]:
                return False  # a locked or removed member's open stream closes
            try:
                _get_run(store, who, ref)
                return True
            except NotFound:
                return False

        def finished() -> bool:
            try:
                return getattr(_get_run(store, who, ref), "status", None) in TERMINAL
            except NotFound:
                return True

        gen = sse_lines(engine, task.id, after, alive, time.sleep, run=task.name, finished=finished)
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @router.post("/api/runs/{ref:path}/nudge", status_code=202)
    def nudge(ref: str, body: dict = Body(...)):
        who = whoami()
        try:
            task = _get_run(store, who, ref)
        except NotFound:
            return nf()
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "text required"}, status_code=400)
        if getattr(task, "status", None) in TERMINAL:
            return JSONResponse({"error": f"{task.name} has ended"}, status_code=409)
        _seen(store, who, task)
        nid = engine.add_nudge(task.id, who.member, text)
        return {"id": nid, "status": "queued"}

    return router
