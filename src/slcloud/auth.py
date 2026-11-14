"""Token auth: hashed bearer tokens, a pure-ASGI middleware and the current principal."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from .store import Store

ROLES = ("admin", "member")
PUBLIC_PATHS = frozenset({"/api/health", "/setup"})
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


@dataclass(frozen=True)
class Principal:
    workspace: str
    role: str
    member: str


_current: ContextVar[Principal | None] = ContextVar("slcloud_principal", default=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_workspace(store: Store, name: str) -> str:
    wid = "w_" + secrets.token_hex(4)
    store.exec("INSERT INTO workspaces(id,name,created) VALUES(?,?,?)", (wid, name, _now()))
    return wid


def create_token(store: Store, workspace: str, role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    token = "slc_" + secrets.token_hex(16)
    store.exec("INSERT INTO tokens(hash,workspace,role,created) VALUES(?,?,?,?)",
               (_hash(token), workspace, role, _now()))
    return token


def check_token(store: Store, token: str) -> Principal | None:
    if not token:
        return None
    h = _hash(token)
    for row in store.query("SELECT hash, workspace, role FROM tokens WHERE hash=?", (h,)):
        if hmac.compare_digest(row["hash"], h):
            return Principal(workspace=row["workspace"], role=row["role"], member=h[:8])
    return None


def current_principal() -> Principal:
    p = _current.get()
    if p is None:
        raise LookupError("no authenticated principal")
    return p


class BearerMiddleware:
    def __init__(self, app, store: Store):
        self.app = app
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and scope.get("method", "GET") not in KNOWN_METHODS:
            body = json.dumps({"error": "not implemented"}).encode()
            await send({"type": "http.response.start", "status": 501, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            return await send({"type": "http.response.body", "body": body})
        if scope["type"] == "http" and scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        principal = None
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                parts = v.decode("latin-1").split(" ", 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    principal = check_token(self.store, parts[1].strip())
                break
        if principal is None:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer")]})
            return await send({"type": "http.response.body", "body": body})
        scope.setdefault("state", {})["principal"] = principal
        reset = _current.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(reset)
