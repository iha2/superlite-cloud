"""Web sign-in, register and sign-out pages, and the session principal."""
from __future__ import annotations

import html
import time
from collections.abc import Callable
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from slcloud import accounts as _accounts
from slcloud.auth import Principal
from slcloud.config import Settings
from slcloud.store import Store

COOKIE = "sid"
MAX_AGE = 12 * 3600
BAD_LOGIN = "Wrong handle or password."
_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY"}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html><head><meta charset=utf-8><title>{html.escape(title)}</title></head>"
           f"<body><h1>{html.escape(title)}</h1>{body}</body></html>")
    return HTMLResponse(doc, status_code=status, headers=_HEADERS)


def _msg(text: str | None) -> str:
    return f"<p role=alert>{html.escape(text)}</p>" if text else ""


def _signin_page(error: str | None = None, status: int = 200) -> HTMLResponse:
    body = (_msg(error) + "<form method=post action=/signin>"
            "<label>Handle <input name=handle autocomplete=username required></label>"
            "<label>Password <input name=password type=password autocomplete=current-password required></label>"
            "<button>Sign in</button></form>")
    return _page("Sign in", body, status)


def _register_page(code: str, info: dict, error: str | None = None, status: int = 200) -> HTMLResponse:
    body = (_msg(error)
            + f"<p>Handle: <b>{html.escape(str(info.get('handle', '')))}</b></p>"
            + f"<p>Email: <b>{html.escape(str(info.get('email') or ''))}</b></p>"
            + "<form method=post action=/register>"
            + f"<input type=hidden name=code value=\"{html.escape(code, quote=True)}\">"
            + "<label>Password (12+ characters) <input name=password type=password "
              "autocomplete=new-password minlength=12 required></label>"
            + "<button>Create account</button></form>")
    return _page("Create your account", body, status)


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _secure(settings: Settings) -> bool:
    return settings.public_url.lower().startswith("https://")


def _set_cookie(resp: Response, sid: str, settings: Settings) -> None:
    resp.set_cookie(COOKIE, sid, max_age=MAX_AGE, httponly=True, samesite="strict",
                    secure=_secure(settings), path="/")


def _go(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303, headers=_HEADERS)


def make_router(store: Store, settings: Settings, accounts=_accounts) -> APIRouter:
    router = APIRouter()

    def forbidden(request: Request) -> HTMLResponse | None:
        if accounts.origin_ok(request.headers.get("origin"), settings.public_url):
            return None
        return _page("Forbidden", _msg("Cross-site request refused."), 403)

    @router.get("/signin")
    async def signin_form() -> HTMLResponse:
        return _signin_page()

    @router.post("/signin")
    async def signin(request: Request):
        if (bad := forbidden(request)) is not None:
            return bad
        f = await _form(request)
        sid = accounts.sign_in(store, f.get("handle", ""), f.get("password", ""), time.time())
        if sid is None:
            return _signin_page(BAD_LOGIN, 401)
        resp = _go("/tokens")
        _set_cookie(resp, sid, settings)
        return resp

    @router.get("/register")
    async def register_form(code: str = ""):
        info = accounts.invite_info(store, code, time.time()) if code else None
        if not info:
            return _page("Invite not valid", _msg("This invite is invalid, used or expired."), 404)
        return _register_page(code, info)

    @router.post("/register")
    async def register(request: Request):
        if (bad := forbidden(request)) is not None:
            return bad
        f = await _form(request)
        code, now = f.get("code", ""), time.time()
        info = accounts.invite_info(store, code, now)
        if not info:
            return _page("Invite not valid", _msg("This invite is invalid, used or expired."), 404)
        try:
            accounts.redeem(store, code, f.get("password", ""), now)
        except ValueError:
            if len(f.get("password", "")) < 12:
                return _register_page(code, info, "Password must be at least 12 characters.", 400)
            return _page("Invite not valid", _msg("This invite is invalid, used or expired."), 404)
        sid = accounts.sign_in(store, info["handle"], f.get("password", ""), now)
        if sid is None:
            return _go("/signin")
        resp = _go("/tokens")
        _set_cookie(resp, sid, settings)
        return resp

    @router.post("/signout")
    async def signout(request: Request):
        if (bad := forbidden(request)) is not None:
            return bad
        sid = request.cookies.get(COOKIE)
        if sid:
            accounts.sign_out(store, sid)
        resp = _go("/signin")
        resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict",
                           secure=_secure(settings))
        return resp

    return router


def session_principal(store: Store, settings: Settings,
                      accounts=_accounts) -> Callable[[Request], Principal | None]:
    def who(request: Request) -> Principal | None:
        sid = request.cookies.get(COOKIE)
        if not sid:
            return None
        member = accounts.session_member(store, sid, time.time())
        if not member:
            return None
        rows = store.query("SELECT workspace, role, handle, locked FROM members WHERE id=?", (member,))
        if not rows or rows[0]["locked"]:
            return None
        r = rows[0]
        return Principal(workspace=r["workspace"], role=r["role"], member=member, handle=r["handle"])

    return who
