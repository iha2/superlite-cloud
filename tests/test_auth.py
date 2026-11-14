import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from slcloud import auth
from slcloud.store import Store


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def ws(store):
    return auth.create_workspace(store, "team")


def test_hash_only_stored(store, ws):
    tok = auth.create_token(store, ws, "admin")
    assert tok.startswith("slc_") and len(tok) == 36
    rows = store.query("SELECT * FROM tokens")
    assert len(rows) == 1
    assert tok not in repr(rows)
    assert len(rows[0]["hash"]) == 64


def test_check_token(store, ws):
    tok = auth.create_token(store, ws, "admin")
    p = auth.check_token(store, tok)
    assert (p.workspace, p.role) == (ws, "admin")
    assert len(p.member) == 8
    assert auth.check_token(store, "slc_wrong") is None
    assert auth.check_token(store, "") is None
    assert ws.startswith("w_")


def test_roles(store, ws):
    with pytest.raises(ValueError):
        auth.create_token(store, ws, "root")
    assert auth.check_token(store, auth.create_token(store, ws, "member")).role == "member"


def _app(store):
    def who(request: Request):
        p = auth.current_principal()
        return JSONResponse({"ws": p.workspace, "role": p.role, "member": p.member,
                             "state": request.state.principal.member})

    def ok(request: Request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/tasks", ok), Route("/api/health", ok),
                            Route("/setup", ok), Route("/mcp/x", who)])
    return auth.BearerMiddleware(app, store)


def test_middleware_401(store, ws):
    c = TestClient(_app(store))
    for h in ({}, {"Authorization": "Bearer slc_bad"}, {"Authorization": "Basic abc"},
              {"Authorization": "Bearer "}):
        r = c.get("/api/tasks", headers=h)
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    tok = auth.create_token(store, ws, "member")
    assert c.get("/api/tasks", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_public_paths(store):
    c = TestClient(_app(store))
    assert c.get("/api/health").status_code == 200
    assert c.get("/setup").status_code == 200


def test_principal_set(store, ws):
    tok = auth.create_token(store, ws, "admin")
    c = TestClient(_app(store))
    r = c.get("/mcp/x", headers={"Authorization": f"Bearer {tok}"})
    p = auth.check_token(store, tok)
    assert r.json() == {"ws": ws, "role": "admin", "member": p.member, "state": p.member}


def test_no_leak_of_token_in_401(store):
    c = TestClient(_app(store))
    r = c.get("/api/tasks", headers={"Authorization": "Bearer slc_secretvalue"})
    assert "slc_secretvalue" not in r.text


def test_header_edge_cases(store, ws):
    tok = auth.create_token(store, ws, "member")
    c = TestClient(_app(store))
    for h in (f"bearer {tok}", f"BEARER {tok}", f"Bearer  {tok} "):
        assert c.get("/api/tasks", headers={"Authorization": h}).status_code == 200
    for h in (tok, f"Bearer{tok}", f"Token {tok}", "Bearer", f"Bearer {tok}x", f"Bearer {tok[:-1]}"):
        assert c.get("/api/tasks", headers={"Authorization": h}).status_code == 401
    assert c.get("/api/tasks", params={"token": tok}).status_code == 401
    assert c.get("/setup/extra").status_code == 401


def test_cross_workspace_isolation(store):
    w1, w2 = auth.create_workspace(store, "a"), auth.create_workspace(store, "b")
    t1, t2 = auth.create_token(store, w1, "admin"), auth.create_token(store, w2, "admin")
    c = TestClient(_app(store))
    assert c.get("/mcp/x", headers={"Authorization": f"Bearer {t1}"}).json()["ws"] == w1
    assert c.get("/mcp/x", headers={"Authorization": f"Bearer {t2}"}).json()["ws"] == w2
    assert auth.check_token(store, t1).workspace != auth.check_token(store, t2).workspace


def test_websocket_closed_1008(store, ws):
    from starlette.websockets import WebSocketDisconnect
    c = TestClient(_app(store))
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws"):
            pass
    assert e.value.code == 1008


def test_principal_not_leaked_after_request(store, ws):
    tok = auth.create_token(store, ws, "admin")
    TestClient(_app(store)).get("/mcp/x", headers={"Authorization": f"Bearer {tok}"})
    with pytest.raises(LookupError):
        auth.current_principal()


def _asgi_status(app, method):
    import asyncio

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": method, "path": "/", "headers": [], "query_string": b""}
    asyncio.run(app(scope, receive, send))
    return sent[0]["status"]


def test_unknown_method_501_before_auth(store):
    c = TestClient(_app(store))
    r = c.request("TRACE", "/")
    assert r.status_code == 501 and r.json() == {"error": "not implemented"}
    assert _asgi_status(_app(store), "get") == 501  # methods are case-sensitive
    assert c.get("/api/tasks").status_code == 401
