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


def sse_lines(engine, task_id: str, after: int, alive: Callable[[], bool],
              sleep: Callable[[float], None], keepalive_s: float = 15.0,
              clock: Callable[[], float] = time.monotonic,
              run: str | None = None) -> Iterator[str]:
    """Yield SSE frames for events after `after` until alive() is False."""
    last = after
    quiet_since = clock()
    while alive():
        fresh = [e for e in engine.events(task_id, after=last) if e["id"] > last]
        if fresh:
            for e in fresh:
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
        hdr = request.headers.get("last-event-id", "")
        if hdr.isdigit():
            after = int(hdr)

        def alive() -> bool:
            try:
                _get_run(store, who, ref)
                return True
            except NotFound:
                return False

        gen = sse_lines(engine, task.id, after, alive, time.sleep, run=task.name)
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
        nid = engine.add_nudge(task.id, who.member, text)
        return {"id": nid, "status": "queued"}

    return router
