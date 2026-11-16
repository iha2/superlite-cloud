import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slcloud import tokens_page
from slcloud.auth import Principal
from slcloud.store import Store

ADMIN = Principal("w_ab12", "admin", "m_9f2c1a7e")
MEMBER = Principal("w_ab12", "member", "m_1a2b3c4d")
URL = "https://cloud.example.com"


class FakeAuth:
    def __init__(self):
        self.tokens = {"m_1a2b3c4d": [{"id": "k_1", "name": "mine", "role": "member", "created": "t", "last_used": None}],
                       "m_9f2c1a7e": [{"id": "k_ab12cd34", "name": "laptop", "role": "admin", "created": "t", "last_used": None}]}
        self.revoked = []

    def create_token(self, store, ws, role, member, name=""):
        self.tokens.setdefault(member, []).append({"id": "k_new", "name": name, "role": role, "created": "t", "last_used": None})
        return "slc_" + "ab" * 16

    def list_tokens(self, store, member):
        return self.tokens.get(member, [])

    def revoke_token(self, store, tid):
        self.revoked.append(tid)
        return True


class FakeMembers:
    def __init__(self):
        self.locked = []

    def list_members(self, store, ws):
        return [{"id": "m_9f2c1a7e", "handle": "chris", "email": "chris@example.com", "role": "admin", "locked": 0},
                {"id": "m_1a2b3c4d", "handle": "pat", "email": "pat@example.com", "role": "member", "locked": 0}]

    def lock(self, store, mid):
        self.locked.append(mid)


class FakeAccounts:
    def __init__(self):
        self.good = True

    def create_invite(self, store, mid, now):
        return "INVITE123"

    def origin_ok(self, origin, public_url):
        return self.good and origin == public_url


@pytest.fixture
def env():
    real = bool(os.environ.get("SUPERLITE_REAL"))
    fa, fm, fc = FakeAuth(), FakeMembers(), FakeAccounts()
    holder = {"who": ADMIN}
    kw = {} if real else dict(auth=fa, members=fm, accounts=fc)
    app = FastAPI()
    app.include_router(tokens_page.make_router(
        Store(":memory:"), SimpleNamespace(public_url=URL), lambda req: holder["who"], **kw))
    c = TestClient(app, headers={"Origin": URL})
    return SimpleNamespace(c=c, who=holder, auth=fa, members=fm, accounts=fc)


def test_token_shown_once(env):
    r = env.c.post("/tokens", json={"name": "laptop"})
    assert r.status_code == 200
    assert f"SUPERLITE_CLOUD_URL={URL}" in r.text
    assert "SUPERLITE_CLOUD_TOKEN=slc_" + "ab" * 16 in r.text
    assert "slc_" not in env.c.get("/tokens").text


def test_admin_only_actions(env):
    env.who["who"] = MEMBER
    assert env.c.post("/invites", data={"member": "m_1a2b3c4d"}).status_code == 403
    assert env.c.get("/members").status_code == 403
    assert env.c.post("/tokens/k_ab12cd34/revoke").status_code == 403
    assert env.c.post("/members/m_9f2c1a7e/lock").status_code == 403
    assert env.members.locked == [] and env.auth.revoked == []
    assert env.c.post("/tokens/k_1/revoke").status_code == 200  # own token


def test_lock_keeps_runs(env):
    r = env.c.post("/members/m_1a2b3c4d/lock")
    assert r.status_code == 200
    assert env.members.locked == ["m_1a2b3c4d"]
    assert env.c.post("/members/m_other/lock").status_code == 404


def test_page_escapes(env):
    r = env.c.post("/tokens", json={"name": "<script>alert(1)</script>"})
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text
    assert "<script>" not in env.c.get("/tokens").text


def test_shows_email(env):
    r = env.c.get("/members")
    assert "chris@example.com" in r.text and "pat@example.com" in r.text


def test_post_checks_origin(env):
    env.accounts.good = False
    assert env.c.post("/tokens", json={"name": "x"}).status_code == 403
    assert env.c.post("/members/m_1a2b3c4d/lock").status_code == 403
    assert env.members.locked == []
    assert env.c.get("/tokens").status_code == 200


def test_invite_and_anonymous(env):
    r = env.c.post("/invites", data={"member": "m_1a2b3c4d"})
    assert "INVITE123" in r.text
    env.who["who"] = None
    assert env.c.get("/tokens").status_code == 401


def test_admin_revoke_stays_in_workspace(env):
    assert env.c.post("/tokens/k_ab12cd34/revoke").status_code == 200
    assert env.c.post("/tokens/k_foreign/revoke").status_code == 404
    assert env.auth.revoked == ["k_ab12cd34"]
