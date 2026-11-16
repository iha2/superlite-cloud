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
    handle: str = ""


_current: ContextVar[Principal | None] = ContextVar("slcloud_principal", default=None)


def _hash(token: str) -> str:
    # surrogatepass: a lone surrogate must hash (and then fail lookup), not raise
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_workspace(store: Store, name: str) -> str:
    wid = "w_" + secrets.token_hex(4)
    store.exec("INSERT INTO workspaces(id,name,created) VALUES(?,?,?)", (wid, name, _now()))
    return wid


def create_token(store: Store, workspace: str, role: str, member: str, name: str = "") -> str:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    token = "slc_" + secrets.token_hex(16)
    store.exec("INSERT INTO tokens(id,hash,workspace,role,member,name,created) VALUES(?,?,?,?,?,?,?)",
               ("k_" + secrets.token_hex(4), _hash(token), workspace, role, member, name, _now()))
    return token


def list_tokens(store: Store, member: str) -> list[dict]:
    return store.query("SELECT id, name, role, created, last_used FROM tokens WHERE member=? ORDER BY created, id",
                       (member,))


def revoke_token(store: Store, token_id: str) -> bool:
    return store.exec("DELETE FROM tokens WHERE id=?", (token_id,)) > 0


def check_token(store: Store, token: str) -> Principal | None:
    if not token:
        return None
    h = _hash(token)
    rows = store.query(
        "SELECT t.id, t.hash, t.workspace, t.role, t.member, m.handle, m.locked FROM tokens t "
        "JOIN members m ON m.id = t.member WHERE t.hash=?", (h,))
    for row in rows:
        if hmac.compare_digest(row["hash"], h):
            if row["locked"]:
                return None  # a locked member's tokens stop working at once
            store.exec("UPDATE tokens SET last_used=? WHERE id=?", (_now(), row["id"]))
            return Principal(workspace=row["workspace"], role=row["role"], member=row["member"],
                             handle=row["handle"])
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
