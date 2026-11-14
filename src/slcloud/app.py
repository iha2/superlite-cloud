"""Wires the store, engine, worker, REST, setup page and MCP into one app."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time

from fastapi import FastAPI

from . import auth, setup
from .api import create_app
from .config import Settings
from .engine import Engine
from .mcp_server import build_mcp
from .sandbox import get_runner
from .store import Store
from .vault import Vault
from .webhooks import Notifier
from .worker import Deps, loop_once


def _parts(settings: Settings, runner=None):
    for d in (settings.data_dir, settings.work_dir):
        os.makedirs(d, exist_ok=True)
    store = Store(settings.db_path)
    vault = Vault(store, settings.master_key)
    engine = Engine(store, notify=Notifier(store))
    runner = runner or get_runner(settings.sandbox, settings.gvisor)
    deps = Deps(engine, vault, runner, settings)
    return store, vault, engine, deps


def build(settings: Settings, runner=None) -> FastAPI:
    store, vault, engine, deps = _parts(settings, runner)
    app = create_app(settings, store, engine, auth.current_principal)
    app.include_router(setup.make_router(vault, settings, auth.current_principal))

    mcp = build_mcp(engine, store, settings)
    # Bearer auth guards every request; host-header rebinding checks would only reject proxies.
    from mcp.server.transport_security import TransportSecuritySettings

    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp_app = mcp.streamable_http_app()  # serves /mcp
    app.mount("/", mcp_app)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    app.add_middleware(auth.BearerMiddleware, store=store)
    app.state.store, app.state.engine, app.state.worker_deps = store, engine, deps
    return app


def init_workspace(settings: Settings, name: str) -> tuple[str, str]:
    os.makedirs(settings.data_dir, exist_ok=True)
    store = Store(settings.db_path)
    ws = auth.create_workspace(store, name)
    return ws, auth.create_token(store, ws, "admin")


def run_worker(settings: Settings, runner=None, poll: float = 2.0, stop=lambda: False) -> None:
    """Claim and run tasks until `stop()` is true or the process is interrupted."""
    *_, deps = _parts(settings, runner)
    try:
        while not stop():
            if not loop_once(deps):
                time.sleep(poll)
    except KeyboardInterrupt:
        pass


class _ProbeServer:
    """HTTPServer-like wrapper (serve_forever, server_address, shutdown, server_close) over uvicorn."""

    def __init__(self, app: FastAPI, tmp: str, port: int = 0):
        import uvicorn

        self._tmp = tmp
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", port))
        self._sock.listen(128)
        self.server_address = self._sock.getsockname()[:2]
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def serve_forever(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True)
                self._thread.start()
            t = self._thread
        t.join()

    def shutdown(self) -> None:
        self._server.should_exit = True
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(10)

    def server_close(self) -> None:
        self.shutdown()
        self._sock.close()
        shutil.rmtree(self._tmp, ignore_errors=True)


def probe_server(port: int = 0) -> _ProbeServer:
    """Zero-argument factory for the probe adapter: a throwaway app on 127.0.0.1 (port 0 = any free port)."""
    tmp = tempfile.mkdtemp(prefix="slcloud-probe-")
    settings = Settings(
        data_dir=tmp, db_path=os.path.join(tmp, "slcloud.db"), work_dir=os.path.join(tmp, "work"),
        master_key=secrets.token_urlsafe(32), sandbox="process")
    app = build(settings)
    store = app.state.store
    token = auth.create_token(store, auth.create_workspace(store, "probe"), "admin")
    return _ProbeServer(_ProbeAuth(app, token), tmp, port)


class _ProbeAuth:
    """Probe-only ASGI wrapper: adds a valid Bearer token to requests that carry no Authorization header."""

    def __init__(self, app, token: str):
        self.app = app
        self._auth = (b"authorization", f"Bearer {token}".encode())

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = list(scope.get("headers", []))
            if not any(k.lower() == b"authorization" for k, _ in headers):
                scope = {**scope, "headers": headers + [self._auth]}
        await self.app(scope, receive, send)
