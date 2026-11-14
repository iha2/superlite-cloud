"""Security probes for token auth: header parsing, revocation, workspace isolation, hashing."""

import hashlib
import inspect

import pytest
from fastapi.testclient import TestClient

from slcloud import app as slapp
from slcloud import auth, learning
from slcloud.config import Settings
from slcloud.store import Store

UNAUTH = {"error": "unauthorized"}


@pytest.fixture
def env(tmp_path):
    s = Settings(data_dir=str(tmp_path / "d"), db_path=str(tmp_path / "d" / "x.db"),
                 work_dir=str(tmp_path / "w"), master_key="k", sandbox="process")
    a = slapp.build(s, runner=object())
    ws_a, tok_a = slapp.init_workspace(s, "a")
    ws_b, tok_b = slapp.init_workspace(s, "b")
    with TestClient(a) as c:
        yield c, a.state.store, (ws_a, tok_a), (ws_b, tok_b)


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _is_401(r):
    return r.status_code == 401 and r.json() == UNAUTH and r.headers.get("www-authenticate") == "Bearer"


def test_missing_token(env):
    c, *_ = env
    assert _is_401(c.get("/api/tasks"))


@pytest.mark.parametrize("value", [
    b"", b"Bearer", b"Bearer ", b"Bearer    ", b"Basic dXNlcjpwYXNz", b"slc_x", b"Token slc_x",
    b"Bearer\tslc_x", b"Bearer slc_\xe9\xff", b"\xff\xfe", b"Bearer slc_x extra",
])
def test_malformed_headers_401(env, value):
    c, *_ = env
    r = c.get("/api/tasks", headers=[(b"authorization", value)])
    assert _is_401(r)


def test_scheme_case_and_spacing_accepted(env):
    c, _, (_, tok), _ = env
    for v in (f"bearer {tok}", f"BEARER {tok}", f"Bearer   {tok}", f"Bearer {tok}  "):
        assert c.get("/api/tasks", headers={"Authorization": v}).status_code == 200, v


def test_invalid_token(env):
    c, _, (_, tok), _ = env
    for bad in ("slc_" + "0" * 32, tok[:-1], tok + "0", tok.upper(), hashlib.sha256(tok.encode()).hexdigest()):
        assert _is_401(c.get("/api/tasks", headers=_h(bad))), bad


def test_check_token_non_ascii_and_surrogate_is_none(tmp_path):
    store = Store(":memory:")
    ws = auth.create_workspace(store, "t")
    auth.create_token(store, ws, "admin")
    assert auth.check_token(store, "slc_é中") is None
    assert auth.check_token(store, "slc_\udcff") is None
    assert auth.check_token(store, None) is None


def test_revoked_token_rejected_immediately_and_on_reuse(env):
    c, store, (_, tok), _ = env
    assert c.get("/api/tasks", headers=_h(tok)).status_code == 200
    store.exec("DELETE FROM tokens WHERE hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))
    for _ in range(2):  # no caching: revoked token stays rejected on every reuse
        assert _is_401(c.get("/api/tasks", headers=_h(tok)))
    assert auth.check_token(store, tok) is None


def test_cross_workspace_is_404_not_403(env):
    c, store, (ws_a, tok_a), (_, tok_b) = env
    tid = c.post("/api/tasks", json={"repo": "/r", "prompt": "p"}, headers=_h(tok_a)).json()["id"]
    lid = learning.propose(store, ws_a, "m", "lesson")["id"]
    probes = [("get", f"/api/tasks/{tid}", None), ("post", f"/api/tasks/{tid}/respond", {"answer": "approve"}),
              ("post", f"/api/tasks/{tid}/cancel", None), ("get", f"/api/tasks/{tid}/events", None),
              ("get", f"/api/tasks/{tid}/bundle", None), ("post", f"/api/lessons/{lid}/approve", None),
              ("post", f"/api/lessons/{lid}/withdraw", None)]
    for method, path, body in probes:
        kw = {"json": body} if body is not None else {}
        other = getattr(c, method)(path, headers=_h(tok_b), **kw)
        missing = getattr(c, method)(path.replace(tid, "t_ffffffffffff").replace(lid, "l_missing"),
                                     headers=_h(tok_b), **kw)
        assert other.status_code == 404, (path, other.status_code)
        assert other.json() == missing.json() == {"error": "not found"}, path
    assert tid not in c.get("/api/tasks", headers=_h(tok_b)).text
    assert c.get(f"/api/tasks/{tid}", headers=_h(tok_a)).json()["status"] == "queued"


def test_tokens_stored_hashed_only(env):
    _, store, (_, tok_a), (_, tok_b) = env
    dump = "\n".join(store._db.iterdump()) if hasattr(store, "_db") else repr(store.query("SELECT * FROM tokens"))
    rows = store.query("SELECT hash FROM tokens")
    assert {r["hash"] for r in rows} == {hashlib.sha256(t.encode()).hexdigest() for t in (tok_a, tok_b)}
    for t in (tok_a, tok_b):
        assert t not in dump and t[4:] not in dump


def test_secret_comparison_is_constant_time_and_hash_keyed():
    src = inspect.getsource(auth.check_token)
    assert "hmac.compare_digest" in src
    assert "_hash(token)" in src  # the lookup key is the digest, never the raw secret
