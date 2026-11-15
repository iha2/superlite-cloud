import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slcloud import keytest, setup
from slcloud.config import Settings

KEY = "sk-ant-secret-WXYZ1234"


class FakeVault:
    def __init__(self):
        self.data = {}

    def set(self, ws, provider, key):
        self.data[(ws, provider)] = key
        return key[-4:]

    def list(self, ws):
        return [{"provider": p, "last4": k[-4:]} for (w, p), k in self.data.items() if w == ws]


def make_vault():
    if os.environ.get("SUPERLITE_REAL") == "1":
        from slcloud.store import Store
        from slcloud.vault import Vault
        return Vault(Store(":memory:"), "m" * 32)
    return FakeVault()


def resp(code, body=None):
    return SimpleNamespace(status_code=code, json=lambda: body or {})


def fake_run(argv, **kw):
    if argv[0] == "cursor":
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    return SimpleNamespace(returncode=0, stdout="2.1.3\n", stderr="")


def client(role="admin", test_key=None, vault=None):
    vault = vault or make_vault()
    s = Settings("/d", "/d/db", "/d/w", "k", public_url="http://h:8787")
    who = lambda: {"role": role, "workspace": "w1"}
    app = FastAPI()
    app.include_router(setup.make_router(
        vault, s, who, run=fake_run,
        test_key=test_key or (lambda p, k: (True, "works"))))
    return TestClient(app), vault


def test_put_key_never_echoes():
    c, v = client()
    r = c.put("/api/keys/anthropic", json={"key": KEY})
    assert r.status_code == 200
    assert r.json() == {"provider": "anthropic", "last4": "1234", "works": True, "message": "works"}
    assert KEY not in r.text
    assert KEY not in c.get("/api/keys").text


def test_failed_key_not_stored():
    c, v = client(test_key=lambda p, k: (False, f"bad key {k}"))
    r = c.put("/api/keys/anthropic", json={"key": KEY})
    assert r.json()["works"] is False
    assert KEY not in r.text
    assert c.get("/api/keys").json() == []


def test_member_cannot_set_key():
    c, v = client(role="member")
    assert c.put("/api/keys/anthropic", json={"key": KEY}).status_code == 403


def test_cursor_not_checked():
    assert keytest.test_key("cursor", "k") == (True, "not checked")


def test_key_works_and_fails():
    seen = {}

    def ok(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return resp(200)

    assert keytest.test_key("anthropic", "k", get=ok) == (True, "works")
    assert seen["url"] == "https://api.anthropic.com/v1/models"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    bad = lambda url, headers=None, timeout=None: resp(401, {"error": {"message": "invalid x-api-key"}})
    assert keytest.test_key("openai", "sk-zzzz", get=bad) == (False, "invalid x-api-key")


def test_agents_and_check():
    c, _ = client()
    a = c.get("/api/agents").json()
    assert a[0]["name"] == "claude" and a[0]["version"] == "2.1.3"
    assert c.post("/api/agents/claude/check").json()["ok"] is True
    assert c.post("/api/agents/cursor/check").json()["ok"] is False
    assert c.post("/api/agents/nope/check").status_code == 404


def test_connect_and_page(monkeypatch):
    monkeypatch.setattr(setup.connect, "connect_command",
                        lambda agent, url, token: f"{agent} {url} {token}")
    c, _ = client()
    r = c.get("/api/connect?agent=claude", headers={"Authorization": "Bearer slc_ab"})
    assert r.json() == {"command": "claude http://h:8787 slc_ab"}
    assert "<html" in c.get("/setup").text


def test_html_no_inline_key():
    html = (Path(setup.__file__).parent / "static" / "setup.html").read_text()
    assert "innerHTML" not in html
    assert "textContent" in html


def test_superlite_version_from_metadata(monkeypatch):
    monkeypatch.setattr(setup.metadata, "version", lambda n: "9.9.9")
    assert setup._superlite_version() == "9.9.9"

    def missing(n):
        raise setup.metadata.PackageNotFoundError(n)

    monkeypatch.setattr(setup.metadata, "version", missing)
    assert setup._superlite_version() == "unknown"


def _built(tmp_path):
    from slcloud import app as slapp
    s = Settings(data_dir=str(tmp_path / "d"), db_path=str(tmp_path / "d" / "x.db"),
                 work_dir=str(tmp_path / "w"), master_key="k", sandbox="process")
    return s, slapp


def test_init_workspace_admin(tmp_path):
    from slcloud import accounts
    from slcloud.store import Store
    s, slapp = _built(tmp_path)
    token, invite = slapp.init_workspace(s, "acme", member="chris")
    assert token.startswith("slc_")
    store = Store(s.db_path)
    rows = store.query("SELECT id, handle, role FROM members")
    assert [(r["handle"], r["role"]) for r in rows] == [("chris", "admin")]
    assert store.query("SELECT member FROM tokens")[0]["member"] == rows[0]["id"]
    import time
    assert accounts.invite_info(store, invite, time.time())["handle"] == "chris"
    _, inv2 = slapp.init_workspace(s, "other")
    assert Store(s.db_path).query("SELECT handle FROM members WHERE handle='other-admin'")


def test_setup_behind_signin(tmp_path):
    s, slapp = _built(tmp_path)
    c = TestClient(slapp.build(s), follow_redirects=False)
    r = c.get("/setup")
    assert r.status_code == 303 and r.headers["location"] == "/signin"
    assert c.get("/signin").status_code == 200
    assert c.get("/api/tasks").status_code == 401


def test_setup_with_session(tmp_path):
    from slcloud import accounts
    s, slapp = _built(tmp_path)
    app = slapp.build(s)
    _, invite = slapp.init_workspace(s, "acme", member="chris")
    store = app.state.store
    import time
    accounts.redeem(store, invite, "correct horse battery", time.time())
    sid = accounts.sign_in(store, "chris", "correct horse battery", time.time())
    c = TestClient(app, follow_redirects=False)
    c.cookies.set("sid", sid)
    assert "<html" in c.get("/setup").text


def test_master_key_notice(tmp_path, caplog):
    s, slapp = _built(tmp_path)
    with caplog.at_level("WARNING", logger="slcloud"):
        with TestClient(slapp.build(s)):
            pass
    assert "master key is not in backups" in caplog.text
