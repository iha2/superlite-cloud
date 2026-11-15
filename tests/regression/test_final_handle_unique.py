"""F-final-3: member handles are unique across all workspaces."""
import io
import time

import pytest
from fastapi.testclient import TestClient

from slcloud import accounts, api, cli, members
from slcloud.config import load_settings
from slcloud.errors import HandleTaken
from slcloud.store import Store

PW = "correct horse battery"


@pytest.fixture
def st(tmp_path):
    return Store(str(tmp_path / "s.db"))


def test_second_workspace_cannot_reuse_handle(st):
    members.create_member(st, "w1", "pat", "pat@example.com", "member")
    with pytest.raises(HandleTaken, match="handle taken"):
        members.create_member(st, "w2", "pat", "pat2@example.com", "member")
    assert len(st.query("SELECT id FROM members WHERE handle='pat'")) == 1


def test_rename_to_taken_handle_in_other_workspace(st):
    members.create_member(st, "w1", "pat", None, "member")
    b = members.create_member(st, "w2", "bob", None, "member")
    with pytest.raises(HandleTaken):
        members.rename(st, b, "pat")


def test_sign_in_reaches_the_one_workspace_with_pat(st):
    mid = members.create_member(st, "w2", "pat", None, "member")
    members.create_member(st, "w1", "chris", None, "admin")
    accounts.redeem(st, accounts.create_invite(st, mid, time.time()), PW, time.time())
    sid = accounts.sign_in(st, "pat", PW, time.time())
    assert sid and accounts.session_member(st, sid, time.time()) == mid


def test_invite_for_taken_handle_fails(tmp_path):
    from slcloud import app as slapp
    s = load_settings({"SLCLOUD_DATA": str(tmp_path), "SLCLOUD_MASTER_KEY": "k", "SLCLOUD_SANDBOX": "process"})
    _, invite = slapp.init_workspace(s, "acme", member="chris")
    store = Store(s.db_path)
    assert accounts.invite_info(store, invite, time.time())["handle"] == "chris"
    with pytest.raises(HandleTaken):
        slapp.init_workspace(s, "other", member="chris")
    assert [r["name"] for r in store.query("SELECT name FROM workspaces")] == ["acme"]
    # default handles are derived per workspace, so two default inits do not clash
    slapp.init_workspace(s, "one")
    slapp.init_workspace(s, "two")


def test_cli_init_and_token_create_say_handle_taken(tmp_path):
    env = {"SLCLOUD_DATA": str(tmp_path), "SLCLOUD_MASTER_KEY": "k"}

    def run(*argv):
        out, err = io.StringIO(), io.StringIO()
        return cli.main(list(argv), env=env, out=out, err=err, server_running=lambda: False), err.getvalue()

    assert run("init", "acme", "--member", "chris")[0] == 0
    code, err = run("init", "other", "--member", "chris")
    assert code == 2 and "handle taken" in err
    store = Store(str(tmp_path / "slcloud.db"))
    store.exec("INSERT INTO workspaces(id,name,created) VALUES('w_other','other','t')")
    code, err = run("token", "create", "other", "member", "--member", "chris", "--email", "c@example.com")
    assert code == 2 and "handle taken" in err


def test_api_maps_handle_taken_to_409(st, tmp_path):
    class W:
        workspace, role, member, handle = "w1", "admin", "m", "x"

    app = api.create_app(load_settings({"SLCLOUD_DATA": str(tmp_path), "SLCLOUD_MASTER_KEY": "k"}), st, None, lambda: W())

    @app.get("/boom")
    def boom():
        raise HandleTaken("pat")

    r = TestClient(app).get("/boom")
    assert r.status_code == 409 and r.json() == {"error": "handle taken"}
