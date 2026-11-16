"""The front dispatcher: cookie pages and bearer routes never stand in for each other."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from slcloud import accounts, auth, members
from slcloud import app as slapp
from slcloud.config import Settings

UNAUTH = {"error": "unauthorized"}
BEARER_ONLY = [
    ("GET", "/api/tasks"), ("GET", "/api/status"), ("POST", "/api/tasks"), ("GET", "/api/lessons"),
    ("POST", "/api/webhooks"), ("GET", "/api/tasks/t_1"), ("GET", "/api/runs/r_1/events"),
    ("POST", "/api/runs/r_1/nudge"), ("POST", "/mcp"), ("GET", "/mcp"), ("POST", "/mcp/"),
]


@pytest.fixture
def env(tmp_path):
    s = Settings(data_dir=str(tmp_path / "d"), db_path=str(tmp_path / "d" / "x.db"),
                 work_dir=str(tmp_path / "w"), master_key="k", sandbox="process")
    a = slapp.build(s, runner=object())
    store = a.state.store
    ws = auth.create_workspace(store, "acme")
    mid = members.create_member(store, ws, "chris", None, "admin")
    token = auth.create_token(store, ws, "admin", mid, "t")
    invite = accounts.create_invite(store, mid, time.time())
    accounts.redeem(store, invite, "correct horse battery", time.time())
    sid = accounts.sign_in(store, "chris", "correct horse battery", time.time())
    assert sid
    with TestClient(a, follow_redirects=False) as c:
        yield c, token, sid


@pytest.mark.parametrize("method,path", BEARER_ONLY)
def test_session_cookie_never_opens_bearer_routes(env, method, path):
    c, _, sid = env
    r = c.request(method, path, cookies={"sid": sid})
    assert r.status_code == 401 and r.json() == UNAUTH


@pytest.mark.parametrize("method,path", [
    ("GET", "/tokens"), ("POST", "/tokens"), ("GET", "/members"), ("POST", "/invites"),
    ("POST", "/members/m_1/lock"), ("POST", "/tokens/k_1/revoke"), ("GET", "/setup")])
def test_bearer_never_opens_cookie_pages(env, method, path):
    c, token, _ = env
    r = c.request(method, path, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (303, 401, 403)
    if r.status_code == 303:
        assert r.headers["location"] == "/signin"
    assert "slc_" not in r.text


def test_cookie_pages_work_with_session(env):
    c, _, sid = env
    assert c.get("/tokens", cookies={"sid": sid}).status_code == 200
    assert c.get("/members", cookies={"sid": sid}).status_code == 200


@pytest.mark.parametrize("path", [
    "/tokensX", "/tokens-evil", "/members-evil", "/membersX", "/invitesX", "/invites/x", "/setupX",
    "/setup/x", "/signinX", "/signin/x", "/registerX", "/signoutX", "/api/../tokens", "/api/../members",
    "/api/tokens", "/x/../tokens", "//tokens", "/%2e%2e/tokens"])
def test_prefix_match_is_exact(path):
    hits = []

    class Stub:
        def __init__(self, name):
            self.name = name

        async def __call__(self, scope, receive, send):
            hits.append((self.name, scope["path"]))

    front = slapp._Front(Stub("api"), Stub("web"))
    asyncio.run(front({"type": "http", "path": path}, None, None))
    assert hits == [("api", path)]


@pytest.mark.parametrize("path", ["/tokens", "/tokens/k_1/revoke", "/members", "/members/m_1/lock",
                                  "/signin", "/register", "/signout", "/setup", "/invites"])
def test_cookie_paths_reach_web(path):
    hits = []

    class Stub:
        def __init__(self, name):
            self.name = name

        async def __call__(self, scope, receive, send):
            hits.append(self.name)

    asyncio.run(slapp._Front(Stub("api"), Stub("web"))({"type": "http", "path": path}, None, None))
    assert hits == ["web"]


@pytest.mark.parametrize("path", ["/tokensX", "/members-evil"])
def test_lookalike_paths_get_bearer_guard(env, path):
    c, _, sid = env
    r = c.get(path, cookies={"sid": sid})
    assert r.status_code == 401 and r.json() == UNAUTH
