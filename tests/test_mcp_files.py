import asyncio
from types import SimpleNamespace

import pytest

from slcloud import files, filestore, mcp_server, members
from slcloud.auth import Principal
from slcloud.engine import Engine
from slcloud.errors import NotFound
from slcloud.store import Store

PLAN = "docs/plans/2026-11-24-login-fix.review.md"
GATE = "9f2c1a7e"
EDIT = "ab12cd34"


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "s.db"))
    eng = Engine(st)
    pat = members.create_member(st, "w_ab12", "pat", "pat@example.com", "member")
    chris = members.create_member(st, "w_ab12", "chris", "chris@example.com", "member")
    who = {"cur": Principal("w_ab12", "member", pat, "pat")}
    t = eng.submit("w_ab12", "https://github.com/acme/app", "Fix the login redirect", "claude",
                   "main", owner=pat, name="login_fix")
    eng.transition(t.id, "running")
    eng.transition(t.id, "waiting")
    filestore.set_gate(st, t.id, "superlite/sessions/login-fix", GATE, True, [PLAN])
    calls = {"read": [], "write": []}

    def read_file(cache, sha, path):
        calls["read"].append(sha)
        if path == "nope.md":
            raise NotFound(path)
        return "text"

    def write_file(cache, tid, gate, path, text):
        calls["write"].append((tid, gate, path, text))
        if path.startswith(".."):
            raise ValueError("bad path")
        return EDIT, [path]

    monkeypatch.setattr(files, "list_files", lambda c, sha: ["README.md", PLAN])
    monkeypatch.setattr(files, "read_file", read_file)
    monkeypatch.setattr(files, "write_file", write_file)
    settings = SimpleNamespace(data_dir=str(tmp_path), public_url="http://h")
    tools = mcp_server.tools(eng, st, settings, lambda: who["cur"])
    return st, eng, t, tools, who, chris, calls, settings


def test_tools_registered(env):
    st, eng, t, tools, who, chris, calls, settings = env
    mcp = mcp_server.build_mcp(eng, st, settings)
    names = {x.name for x in asyncio.run(mcp.list_tools())}
    assert {"list_files", "read_file", "write_file"} <= names
    assert {"submit_task", "get_task", "list_tasks", "respond", "cancel_task", "fetch_command",
            "team_lessons", "list_runs", "give_run", "status"} <= names


def test_write_then_files_shows_edit(env):
    st, eng, t, tools, who, chris, calls, settings = env
    assert tools["list_files"](ref="login_fix") == {"files": ["README.md", PLAN]}
    assert tools["write_file"](ref="login_fix", path=PLAN, text="# v2\n") == {"sha": EDIT, "files": [PLAN]}
    assert calls["write"] == [(t.id, GATE, PLAN, "# v2\n")]
    assert filestore.get(st, t.id)["edits"] == {"sha": EDIT, "files": [PLAN]}
    tools["read_file"](ref="login_fix", path="README.md")
    assert calls["read"] == [EDIT]
    assert tools["read_file"](ref="login_fix", path="nope.md") == {"error": "not found"}


def test_not_waiting_error(env):
    st, eng, t, tools, who, chris, calls, settings = env
    eng.transition(t.id, "cancelled")
    err = {"error": "run is cancelled, not waiting"}
    assert tools["list_files"](ref="login_fix") == err
    assert tools["read_file"](ref="login_fix", path="README.md") == err
    assert tools["write_file"](ref="login_fix", path="README.md", text="x") == err
    assert calls["write"] == []


def test_path_escape_error(env):
    st, eng, t, tools, who, chris, calls, settings = env
    assert tools["write_file"](ref="login_fix", path="../x", text="x") == {"error": "bad path"}
    assert filestore.get(st, t.id)["edits"] is None


def test_other_member_not_found(env):
    st, eng, t, tools, who, chris, calls, settings = env
    who["cur"] = Principal("w_ab12", "member", chris, "chris")
    err = {"error": "not found"}
    assert tools["list_files"](ref="login_fix") == err
    assert tools["list_files"](ref="pat/login_fix") == err
    assert tools["write_file"](ref=t.id, path="README.md", text="x") == err
    assert filestore.get(st, t.id)["edits"] is None


def test_not_published(env):
    st, eng, t, tools, who, chris, calls, settings = env
    filestore.clear(st, t.id)
    assert tools["list_files"](ref="login_fix") == {"error": "files not published yet"}
