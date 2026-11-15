import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from slcloud import auth
from slcloud.store import Store

M = "m_9f2c1a7e"


@pytest.fixture
def store():
    return Store(":memory:")


def add_member(store, ws, mid):
    store.exec("INSERT INTO members(id,workspace,handle,role) VALUES(?,?,?,?)", (mid, ws, "h" + mid, "member"))


@pytest.fixture
def ws(store):
    w = auth.create_workspace(store, "team")
    for mid in (M, "m_other"):
        add_member(store, w, mid)
    return w


def test_hash_only_stored(store, ws):
    tok = auth.create_token(store, ws, "admin", M)
    assert tok.startswith("slc_") and len(tok) == 36
    rows = store.query("SELECT * FROM tokens")
    assert len(rows) == 1
    assert tok not in repr(rows)
    assert len(rows[0]["hash"]) == 64


def test_check_token(store, ws):
    tok = auth.create_token(store, ws, "admin", M)
    p = auth.check_token(store, tok)
    assert (p.workspace, p.role) == (ws, "admin")
    assert p.member == M
    assert auth.check_token(store, "slc_wrong") is None
    assert auth.check_token(store, "") is None
    assert ws.startswith("w_")


def test_roles(store, ws):
    with pytest.raises(ValueError):
        auth.create_token(store, ws, "root", M)
    assert auth.check_token(store, auth.create_token(store, ws, "member", M)).role == "member"


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
    tok = auth.create_token(store, ws, "member", M)
    assert c.get("/api/tasks", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_public_paths(store):
    c = TestClient(_app(store))
    assert c.get("/api/health").status_code == 200
    assert c.get("/setup").status_code == 200


def test_principal_set(store, ws):
    tok = auth.create_token(store, ws, "admin", M)
    c = TestClient(_app(store))
    r = c.get("/mcp/x", headers={"Authorization": f"Bearer {tok}"})
    p = auth.check_token(store, tok)
    assert r.json() == {"ws": ws, "role": "admin", "member": p.member, "state": p.member}


def test_no_leak_of_token_in_401(store):
    c = TestClient(_app(store))
    r = c.get("/api/tasks", headers={"Authorization": "Bearer slc_secretvalue"})
    assert "slc_secretvalue" not in r.text


def test_header_edge_cases(store, ws):
    tok = auth.create_token(store, ws, "member", M)
    c = TestClient(_app(store))
    for h in (f"bearer {tok}", f"BEARER {tok}", f"Bearer  {tok} "):
        assert c.get("/api/tasks", headers={"Authorization": h}).status_code == 200
    for h in (tok, f"Bearer{tok}", f"Token {tok}", "Bearer", f"Bearer {tok}x", f"Bearer {tok[:-1]}"):
        assert c.get("/api/tasks", headers={"Authorization": h}).status_code == 401
    assert c.get("/api/tasks", params={"token": tok}).status_code == 401
    assert c.get("/setup/extra").status_code == 401


def test_cross_workspace_isolation(store):
    w1, w2 = auth.create_workspace(store, "a"), auth.create_workspace(store, "b")
    add_member(store, w1, M)
    add_member(store, w2, "m_2")
    t1, t2 = auth.create_token(store, w1, "admin", M), auth.create_token(store, w2, "admin", "m_2")
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
    tok = auth.create_token(store, ws, "admin", M)
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


def _member(store, ws, handle="chris", locked=0):
    store.exec("UPDATE members SET handle=?, email=?, role='admin', locked=? WHERE id=? AND workspace=?",
               (handle, f"{handle}@example.com", locked, M, ws))


def test_check_token_member(store, ws):
    _member(store, ws)
    tok = auth.create_token(store, ws, "admin", M, "laptop")
    assert store.query("SELECT last_used FROM tokens")[0]["last_used"] is None
    p = auth.check_token(store, tok)
    assert (p.member, p.handle, p.workspace, p.role) == (M, "chris", ws, "admin")
    assert store.query("SELECT last_used FROM tokens")[0]["last_used"]


def test_locked_member_token_rejected(store, ws):
    _member(store, ws, locked=1)
    assert auth.check_token(store, auth.create_token(store, ws, "admin", M)) is None


def test_revoke_token(store, ws):
    tok = auth.create_token(store, ws, "member", M, "laptop")
    tid = auth.list_tokens(store, M)[0]["id"]
    assert tid.startswith("k_") and len(tid) == 10
    assert auth.revoke_token(store, tid) is True
    assert auth.check_token(store, tok) is None
    assert auth.revoke_token(store, tid) is False


def test_list_no_hash(store, ws):
    tok = auth.create_token(store, ws, "member", M, "laptop")
    auth.create_token(store, ws, "member", "m_other", "x")
    rows = auth.list_tokens(store, M)
    assert len(rows) == 1 and set(rows[0]) == {"id", "name", "role", "created", "last_used"}
    assert rows[0]["name"] == "laptop" and rows[0]["last_used"] is None
    assert tok not in repr(rows) and auth._hash(tok) not in repr(rows)


def test_token_not_logged(store, ws, caplog, capsys):
    import logging

    with caplog.at_level(logging.DEBUG):
        tok = auth.create_token(store, ws, "member", M, "laptop")
    out = capsys.readouterr()
    assert tok not in caplog.text + out.out + out.err


def test_token_for_missing_member_rejected(store, ws):
    tok = auth.create_token(store, ws, "admin", "m_nosuch")
    assert auth.check_token(store, tok) is None

