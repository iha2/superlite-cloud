"""Setup page and key/agent REST routes."""

from __future__ import annotations

import subprocess
from importlib import metadata
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import connect, keytest
from .vault import mask

AGENTS = ("claude", "codex", "gemini", "cursor")
PROVIDERS = ("anthropic", "openai", "gemini", "cursor")
_HTML = Path(__file__).parent / "static" / "setup.html"


def _field(p, name, default=None):
    if isinstance(p, dict):
        return p.get(name, default)
    return getattr(p, name, default)


def _superlite_version() -> str:
    try:
        return metadata.version("superlite")
    except metadata.PackageNotFoundError:
        return "unknown"


def make_router(vault, settings, whoami, run=subprocess.run, test_key=keytest.test_key) -> APIRouter:
    r = APIRouter()

    def _line(argv: list[str]) -> str | None:
        try:
            p = run(argv, capture_output=True, text=True, timeout=15)
        except Exception:
            return None
        if getattr(p, "returncode", 1) != 0:
            return None
        out = (p.stdout or "").strip().splitlines()
        return out[0].strip() if out else ""

    def _info(name: str) -> dict:
        return {
            "name": name,
            "version": _line([name, "--version"]),
            "superlite": _superlite_version() if name == "claude" else None,
        }

    @r.get("/setup", response_class=HTMLResponse)
    def page():
        return HTMLResponse(_HTML.read_text(encoding="utf-8"))

    @r.get("/api/agents")
    def agents(who=Depends(whoami)):
        return [_info(n) for n in AGENTS]

    @r.post("/api/agents/{name}/check")
    def check(name: str, who=Depends(whoami)):
        if name not in AGENTS:
            raise HTTPException(404, "unknown agent")
        info = _info(name)
        info["ok"] = info["version"] is not None
        return info

    @r.get("/api/keys")
    def keys(who=Depends(whoami)):
        return vault.list(_field(who, "workspace"))

    @r.put("/api/keys/{provider}")
    async def put_key(provider: str, request: Request, who=Depends(whoami)):
        if _field(who, "role") != "admin":
            raise HTTPException(403, "admin only")
        if provider not in PROVIDERS:
            raise HTTPException(404, "unknown provider")
        body = await request.json()
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            raise HTTPException(422, "key required")
        works, message = test_key(provider, key)
        message = mask(message, [key])
        if not works:
            return {"provider": provider, "last4": None, "works": False, "message": message}
        last4 = vault.set(_field(who, "workspace"), provider, key)
        return {"provider": provider, "last4": last4, "works": True, "message": message}

    @r.get("/api/connect")
    def connect_(request: Request, agent: str = "claude", who=Depends(whoami)):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else "<your-token>"
        return {"command": connect.connect_command(agent, settings.public_url, token)}

    return r
