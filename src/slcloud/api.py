"""REST API."""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from . import learning, members, runs
from .config import Settings
from .files_api import make_files_router
from .errors import BadTransition, Forbidden, HandleTaken, NotFound, Rejected
from .store import Store
from .stream import make_router


def create_app(settings: Settings, store: Store, engine, whoami) -> FastAPI:
    app = FastAPI(title="slcloud")
    app.include_router(make_router(store, engine, whoami))
    # before the greedy /api/tasks/{ref:path} and /bundle routes below
    app.include_router(make_files_router(store, engine, whoami, settings))

    def err(status: int, msg: str) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=status)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound):
        return err(404, "not found")

    @app.exception_handler(runs.NoSuchMember)
    async def _nm(_: Request, e: runs.NoSuchMember):
        return err(404, "no such member")

    @app.exception_handler(BadTransition)
    async def _bt(_: Request, e: BadTransition):
        return err(409, str(e) or "bad transition")

    @app.exception_handler(HandleTaken)
    async def _ht(_: Request, e: HandleTaken):
        return err(409, "handle taken")

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

    def run_of(ref: str):
        return runs.get_run(store, whoami(), ref)

    @app.post("/api/tasks")
    def submit(body: dict = Body(...)):
        p = whoami()
        try:
            repo, prompt = body["repo"], body["prompt"]
        except KeyError as e:
            raise Rejected(f"missing field: {e.args[0]}") from None
        # the owner is always the caller; a body "owner" is ignored
        try:
            t = engine.submit(p.workspace, repo, prompt, body.get("agent", "claude"), body.get("base", "HEAD"),
                              owner=p.member, name=body.get("name"),
                              orchestrator=body.get("orchestrator", "ai"))
        except ValueError as e:  # bad name or unknown agent: the client's mistake, not a 500
            raise Rejected(str(e)) from None
        return t.public()

    @app.get("/api/tasks")
    def list_tasks(all: int = 0):
        p = whoami()
        if all:
            admin()
            out = [t.public() for t in engine.list(p.workspace)]
        else:
            out = [t.public() for t in engine.list(p.workspace, owner=p.member)]
        runs.mark_seen(store, p.member)
        return out

    @app.get("/api/me")
    def me():
        return runs.me(store, whoami())

    @app.get("/api/status")
    def status():
        p = whoami()  # reading status never marks notices seen
        return {"notices": runs.notices(store, p.member), "runs": runs.live_runs(store, p)}

    @app.post("/api/runs/{ref:path}/give")
    def give(ref: str, body: dict = Body(...)):
        email = body.get("email")
        if not email or not isinstance(email, str):
            raise Rejected("missing field: email")
        out = runs.give(store, engine, whoami(), ref, email).public()
        target = members.member_by_email(store, email)  # give() already proved it exists
        if target:
            out.update(to_handle=target["handle"], to_email=target["email"])
        return out

    @app.get("/api/runs/{ref:path}/attach")
    def attach(ref: str):
        t = run_of(ref)
        runs.mark_seen_run(store, whoami().member, t.id)
        return {"run": t.name, "id": t.id, "status": t.status}

    @app.post("/api/tasks/{ref:path}/respond")
    def respond(ref: str, body: dict = Body(...)):
        t = run_of(ref)
        if "answer" not in body:
            raise Rejected("missing field: answer")
        return engine.respond(whoami().workspace, t.id, body["answer"]).public()

    @app.post("/api/tasks/{ref:path}/cancel")
    def cancel(ref: str):
        t = run_of(ref)
        return engine.cancel(whoami().workspace, t.id).public()

    @app.get("/api/tasks/{ref:path}/events")
    def events(ref: str):
        return engine.events(run_of(ref).id)

    @app.get("/api/tasks/{ref:path}/bundle")
    def bundle(ref: str):
        t = run_of(ref)
        path = os.path.join(settings.data_dir, "bundles", f"{t.id}.bundle")
        if not os.path.isfile(path):
            raise NotFound("bundle")
        return FileResponse(path, media_type="application/octet-stream")

    @app.get("/api/tasks/{ref:path}")
    def get_task(ref: str):
        return run_of(ref).public()

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
