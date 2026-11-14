"""REST API."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from . import learning
from .config import Settings
from .errors import BadTransition, Forbidden, NotFound, Rejected
from .store import Store


def create_app(settings: Settings, store: Store, engine, whoami) -> FastAPI:
    app = FastAPI(title="slcloud")

    def err(status: int, msg: str) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=status)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return err(404, "not found")

    @app.exception_handler(BadTransition)
    async def _bt(_: Request, e: BadTransition):
        return err(409, str(e) or "bad transition")

    @app.exception_handler(Rejected)
    async def _rj(_: Request, e: Rejected):
        return err(400, str(e) or "rejected")

    @app.exception_handler(Forbidden)
    async def _fb(_: Request, e: Forbidden):
        return err(403, str(e) or "forbidden")

    def admin():
        p = whoami()
        if p.role != "admin":
            raise Forbidden("admin only")
        return p

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/tasks")
    def submit(body: dict = Body(...)):
        p = whoami()
        try:
            repo, prompt = body["repo"], body["prompt"]
        except KeyError as e:
            raise Rejected(f"missing field: {e.args[0]}") from None
        t = engine.submit(p.workspace, repo, prompt, body.get("agent", "claude"), body.get("base", "HEAD"))
        return t.public()

    @app.get("/api/tasks")
    def list_tasks():
        return [t.public() for t in engine.list(whoami().workspace)]

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        return engine.get(whoami().workspace, task_id).public()

    @app.post("/api/tasks/{task_id}/respond")
    def respond(task_id: str, body: dict = Body(...)):
        if "answer" not in body:
            raise Rejected("missing field: answer")
        return engine.respond(whoami().workspace, task_id, body["answer"]).public()

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return engine.cancel(whoami().workspace, task_id).public()

    @app.get("/api/tasks/{task_id}/events")
    def events(task_id: str):
        ws = whoami().workspace
        engine.get(ws, task_id)
        return engine.events(task_id)

    @app.get("/api/tasks/{task_id}/bundle")
    def bundle(task_id: str):
        engine.get(whoami().workspace, task_id)
        path = os.path.join(settings.data_dir, "bundles", f"{task_id}.bundle")
        if not os.path.isfile(path):
            raise NotFound("bundle")
        return FileResponse(path, media_type="application/octet-stream")

    @app.post("/api/learn/rows")
    def learn_rows(body: dict = Body(...)):
        p = whoami()
        rows = body.get("rows")
        if not isinstance(rows, list):
            raise Rejected("rows must be a list")
        return learning.ingest(store, p.workspace, p.member, rows)

    @app.get("/api/learn/totals")
    def learn_totals():
        return learning.totals(store, whoami().workspace)

    @app.get("/api/lessons")
    def get_lessons():
        return learning.lessons(store, whoami().workspace)

    @app.post("/api/lessons")
    def propose(body: dict = Body(...)):
        p = whoami()
        if not body.get("text"):
            raise Rejected("missing field: text")
        return learning.propose(store, p.workspace, p.member, body["text"])

    @app.post("/api/lessons/{lesson_id}/approve")
    def approve(lesson_id: str):
        p = admin()
        return learning.approve(store, p.workspace, lesson_id, p.member)

    @app.post("/api/lessons/{lesson_id}/withdraw")
    def withdraw(lesson_id: str):
        p = admin()
        return learning.withdraw(store, p.workspace, lesson_id)

    @app.post("/api/webhooks")
    def add_webhook(body: dict = Body(...)):
        p = admin()
        url, secret = body.get("url"), body.get("secret")
        if not url or not str(url).startswith(("http://", "https://")):
            raise Rejected("url must be http(s)")
        if not secret:
            raise Rejected("missing field: secret")
        hid = "wh_" + secrets.token_hex(6)
        store.exec(
            "INSERT INTO webhooks(id,workspace,url,secret) VALUES(?,?,?,?)",
            (hid, p.workspace, url, secret),
        )
        return {"id": hid, "url": url}

    return app
