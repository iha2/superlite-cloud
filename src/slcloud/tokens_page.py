"""Token, member and invite pages: HTML routes behind a session or bearer principal."""

from __future__ import annotations

import html
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from fastapi import APIRouter, Request

from slcloud import accounts as _accounts
from slcloud import auth as _auth
from slcloud import members as _members

if TYPE_CHECKING:
    from slcloud.config import Settings
    from slcloud.store import Store


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _page(title: str, body: str):
    from fastapi.responses import HTMLResponse

    doc = (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{_e(title)}</title></head>"
           f"<body><h1>{_e(title)}</h1>{body}</body></html>")
    return HTMLResponse(doc, headers={"Cache-Control": "no-store"})


def _err(status: int, message: str):
    from fastapi.responses import JSONResponse

    return JSONResponse({"error": message}, status_code=status)


async def _fields(request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    if "json" in request.headers.get("content-type", ""):
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}


def make_router(store: Store, settings: Settings, who: Callable, auth=_auth, members=_members,
                accounts=_accounts) -> APIRouter:
    router = APIRouter()

    def gate(request: Request, *, post: bool, admin: bool = False):
        """Returns (principal, None) or (None, error response)."""
        p = who(request)
        if p is None:
            return None, _err(401, "unauthorized")
        if post and not accounts.origin_ok(request.headers.get("origin"), settings.public_url):
            return None, _err(403, "forbidden")
        if admin and p.role != "admin":
            return None, _err(403, "forbidden")
        return p, None

    def token_rows(member: str) -> str:
        rows = "".join(
            f"<tr><td>{_e(t.get('name'))}</td><td>{_e(t.get('role'))}</td><td>{_e(t.get('created'))}</td>"
            f"<td>{_e(t.get('last_used') or 'never')}</td>"
            f"<td><form method=\"post\" action=\"/tokens/{_e(t['id'])}/revoke\">"
            f"<button>Revoke</button></form></td></tr>"
            for t in auth.list_tokens(store, member))
        return f"<table><tr><th>Name</th><th>Role</th><th>Created</th><th>Last used</th><th></th></tr>{rows}</table>"

    def tokens_form() -> str:
        return ("<form method=\"post\" action=\"/tokens\"><input name=\"name\" maxlength=\"60\">"
                "<button>Create token</button></form>")

    @router.get("/tokens")
    async def tokens_list(request: Request):
        p, bad = gate(request, post=False)
        if bad:
            return bad
        return _page("Tokens", tokens_form() + token_rows(p.member))

    @router.post("/tokens")
    async def tokens_create(request: Request):
        p, bad = gate(request, post=True)
        if bad:
            return bad
        name = (await _fields(request)).get("name", "")[:60]
        token = auth.create_token(store, p.workspace, p.role, p.member, name)
        shown = (f"<p>Copy these now. The token is shown once.</p><pre>SUPERLITE_CLOUD_URL={_e(settings.public_url)}\n"
                 f"SUPERLITE_CLOUD_TOKEN={_e(token)}</pre>")
        return _page("Token created", shown + tokens_form() + token_rows(p.member))

    @router.post("/tokens/{token_id}/revoke")
    async def tokens_revoke(token_id: str, request: Request):
        p, bad = gate(request, post=True)
        if bad:
            return bad
        if p.role != "admin":
            if token_id not in {t["id"] for t in auth.list_tokens(store, p.member)}:
                return _err(403, "forbidden")
        elif not any(t["id"] == token_id for m in members.list_members(store, p.workspace)
                     for t in auth.list_tokens(store, m["id"])):
            return _err(404, "not found")  # an admin reaches only their own workspace's tokens
        if not auth.revoke_token(store, token_id):
            return _err(404, "not found")
        return _page("Token revoked", token_rows(p.member))

    def member_table(ws: str) -> str:
        rows = "".join(
            f"<tr><td>{_e(m.get('handle'))}</td><td>{_e(m.get('email'))}</td><td>{_e(m.get('role'))}</td>"
            f"<td>{'locked' if m.get('locked') else 'active'}</td><td>"
            f"<form method=\"post\" action=\"/members/{_e(m['id'])}/lock\"><button>Lock</button></form>"
            f"<form method=\"post\" action=\"/invites\"><input type=\"hidden\" name=\"member\" value=\"{_e(m['id'])}\">"
            f"<button>Invite</button></form></td></tr>"
            for m in members.list_members(store, ws))
        return f"<table><tr><th>Handle</th><th>Email</th><th>Role</th><th>State</th><th></th></tr>{rows}</table>"

    def in_workspace(ws: str, member_id: str) -> bool:
        return any(m["id"] == member_id for m in members.list_members(store, ws))

    @router.get("/members")
    async def members_list(request: Request):
        p, bad = gate(request, post=False, admin=True)
        if bad:
            return bad
        return _page("Members", member_table(p.workspace))

    @router.post("/members/{member_id}/lock")
    async def members_lock(member_id: str, request: Request):
        p, bad = gate(request, post=True, admin=True)
        if bad:
            return bad
        if not in_workspace(p.workspace, member_id):
            return _err(404, "not found")
        members.lock(store, member_id)  # runs stay: only sign-in and tokens are cut
        return _page("Members", member_table(p.workspace))

    @router.post("/invites")
    async def invites_create(request: Request):
        p, bad = gate(request, post=True, admin=True)
        if bad:
            return bad
        member_id = (await _fields(request)).get("member", "")
        if not in_workspace(p.workspace, member_id):
            return _err(404, "not found")
        code = accounts.create_invite(store, member_id, time.time())
        return _page("Invite", f"<p>Send this code once.</p><pre>{_e(code)}</pre>")

    return router
