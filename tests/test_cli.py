import io
import json

import pytest

from slcloud import cli, connect, learning

URL = "http://h:8787"


class Poster:
    def __init__(self, status=200, body=None):
        self.calls = []
        self.status = status
        self.body = body if body is not None else {}

    def __call__(self, method, url, token, body):
        self.calls.append((method, url, token, body))
        return self.status, self.body


@pytest.fixture
def outbox(monkeypatch):
    """Fake outbox_rows by default; the real one with SUPERLITE_REAL=1."""
    import os

    if os.environ.get("SUPERLITE_REAL"):
        return None
    row = {"v": 1, "id": "a", "signal": "rework", "date": "2026-11-14"}

    def fake(path, offset):
        return ([row], 58) if offset == 0 else ([], offset)

    monkeypatch.setattr(learning, "outbox_rows", fake)
    return fake


def run(argv, post=None, stdin="", tmp=None):
    out, err = io.StringIO(), io.StringIO()
    env = {"SLCLOUD_URL": URL, "SLCLOUD_TOKEN": "slc_ab", "HOME": str(tmp or "/nonexistent")}
    code = cli.main(argv, http=post, env=env, stdin=io.StringIO(stdin), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_claude_connect():
    assert connect.connect_command("claude", URL, "slc_ab") == (
        'claude mcp add --transport http slcloud http://h:8787/mcp --header "Authorization: Bearer slc_ab"'
    )


def test_codex_connect():
    assert connect.connect_command("codex", URL, "slc_ab") == (
        "export SLCLOUD_TOKEN=slc_ab && codex mcp add slcloud --url http://h:8787/mcp "
        "--bearer-token-env-var SLCLOUD_TOKEN"
    )


def test_gemini_connect():
    assert connect.connect_command("gemini", URL, "slc_ab") == (
        'gemini mcp add --transport http slcloud http://h:8787/mcp --header "Authorization: Bearer slc_ab"'
    )


def test_cursor_connect():
    data = json.loads(connect.connect_command("cursor", URL, "slc_ab"))
    assert data == {
        "mcpServers": {
            "slcloud": {"url": "http://h:8787/mcp", "headers": {"Authorization": "Bearer slc_ab"}}
        }
    }


def test_connect_cli(tmp_path):
    code, out, _ = run(["connect", "gemini"], tmp=tmp_path)
    assert code == 0
    assert out.strip() == connect.connect_command("gemini", URL, "slc_ab")


def test_sync_once(tmp_path, outbox):
    post = Poster(200)
    code, _, _ = run(["sync"], post, tmp=tmp_path)
    assert code == 0
    assert len(post.calls) == 1
    method, url, token, body = post.calls[0]
    assert (method, url, token) == ("POST", f"{URL}/api/learn/rows", "slc_ab")
    assert body["rows"][0]["id"] == "a"
    state = json.loads((tmp_path / ".config/slcloud/sync.json").read_text())
    assert state[URL] == 58
    code, _, _ = run(["sync"], post, tmp=tmp_path)
    assert code == 0
    assert len(post.calls) == 1


def test_sync_failure_keeps_offset(tmp_path, outbox):
    post = Poster(500)
    code, _, _ = run(["sync"], post, tmp=tmp_path)
    assert code == 1
    assert not (tmp_path / ".config/slcloud/sync.json").exists()
    ok = Poster(200)
    run(["sync"], ok, tmp=tmp_path)
    assert len(ok.calls) == 1


def _real_outbox(tmp_path, monkeypatch, rows):
    import os

    monkeypatch.undo()
    f = tmp_path / "outbox.jsonl"
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return f


def _run_real(tmp_path, f, post):
    out, err = io.StringIO(), io.StringIO()
    env = {"SLCLOUD_URL": URL, "SLCLOUD_TOKEN": "slc_ab", "HOME": str(tmp_path), "SLCLOUD_OUTBOX": str(f)}
    code = cli.main(["sync"], http=post, env=env, stdin=io.StringIO(""), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


GOOD = {"v": 1, "id": "a", "signal": "rework", "date": "2026-11-14"}


def test_sync_skips_bad_row(tmp_path, monkeypatch):
    bad = dict(GOOD, id="b", cmd="rm -rf /")
    f = _real_outbox(tmp_path, monkeypatch, [GOOD, bad])
    post = Poster(200)
    code, out, err = _run_real(tmp_path, f, post)
    assert code == 0
    assert len(post.calls) == 1
    assert [r["id"] for r in post.calls[0][3]["rows"]] == ["a"]
    assert "skipped 1 rows" in err
    assert "rm -rf" not in out + err
    state = json.loads((tmp_path / ".config/slcloud/sync.json").read_text())
    assert state[URL] == f.stat().st_size
    code, out, err = _run_real(tmp_path, f, post)
    assert code == 0
    assert len(post.calls) == 1
    assert "nothing to sync" in out


def test_sync_only_bad_rows(tmp_path, monkeypatch):
    bad = dict(GOOD, cmd="rm -rf /")
    f = _real_outbox(tmp_path, monkeypatch, [bad])
    post = Poster(200)
    code, out, err = _run_real(tmp_path, f, post)
    assert code == 0
    assert post.calls == []
    assert "skipped 1 rows" in err and "nothing to sync" in out
    assert "rm -rf" not in out + err
    state = json.loads((tmp_path / ".config/slcloud/sync.json").read_text())
    assert state[URL] == f.stat().st_size


def test_key_set_reads_stdin(tmp_path):
    post = Poster(200, {"provider": "anthropic", "last4": "cdef", "works": True, "message": "ok"})
    code, out, err = run(["key", "set", "anthropic"], post, stdin="sk-secret-abcdef\n", tmp=tmp_path)
    assert code == 0
    method, url, token, body = post.calls[0]
    assert (method, url, body) == ("PUT", f"{URL}/api/keys/anthropic", {"key": "sk-secret-abcdef"})
    assert "sk-secret" not in out + err
    assert "cdef" in out


def test_key_set_failure_hides_key(tmp_path):
    post = Poster(400, {"error": "bad key sk-secret-abcdef"})
    code, out, err = run(["key", "set", "anthropic"], post, stdin="sk-secret-abcdef\n", tmp=tmp_path)
    assert code == 1
    assert "sk-secret" not in out + err


def test_key_set_rejected_by_provider(tmp_path):
    post = Poster(200, {"provider": "anthropic", "last4": None, "works": False, "message": "invalid x-api-key"})
    code, out, err = run(["key", "set", "anthropic"], post, stdin="sk-secret-abcdef\n", tmp=tmp_path)
    assert code == 1 and out == ""
    assert "anthropic key rejected by provider; nothing stored" in err
    assert "None" not in err and "sk-secret" not in err


def test_unknown_command():
    code, _, _ = run(["bogus"])
    assert code == 2
    assert run([])[0] == 2



# ---- admin commands (app, members, accounts, auth faked) ----

CHRIS = {"id": "m_c", "workspace": "w_ab12", "handle": "chris", "email": "chris@example.com", "role": "admin", "locked": 0}
PAT = {"id": "m_p", "workspace": "w_ab12", "handle": "pat", "email": None, "role": "member", "locked": 0}


def admin(argv, tmp_path, monkeypatch, have=(CHRIS, PAT), server_running=lambda: False):
    from slcloud import accounts, app, auth, members
    from slcloud.store import Store

    state = {"members": {m["handle"]: dict(m) for m in have}, "tokens": []}

    def create_member(store, ws, handle, email, role):
        state["members"][handle] = {"id": "m_" + handle, "workspace": ws, "handle": handle, "email": email,
                                    "role": role, "locked": 0}
        return "m_" + handle

    def fake(mod, name, fn):
        monkeypatch.setattr(mod, name, fn, raising=False)

    fake(app, "init_workspace", lambda s, name, member="admin": ("slc_tok", "INV1"))
    fake(members, "create_member", create_member)
    fake(members, "member_by_handle", lambda st, ws, h: state["members"].get(h))
    fake(members, "list_members", lambda st, ws: list(state["members"].values()))
    fake(members, "set_email", lambda st, mid, e: state.setdefault("email", (mid, e)))
    fake(accounts, "create_invite", lambda st, mid, now: "INV2")
    fake(auth, "create_token",
         lambda st, ws, role, member, name="": state["tokens"].append((ws, role, member)) or "slc_new")
    Store(str(tmp_path / "slcloud.db")).exec("INSERT OR IGNORE INTO workspaces(id,name,created) VALUES('w_ab12','acme','t')")
    out, err = io.StringIO(), io.StringIO()
    env = {"SLCLOUD_DATA": str(tmp_path), "SLCLOUD_MASTER_KEY": "k", "HOME": str(tmp_path)}
    code = cli.main(argv, env=env, out=out, err=err, server_running=server_running)
    return code, out.getvalue(), err.getvalue(), state


def test_init_prints_token_and_invite(tmp_path, monkeypatch):
    code, out, _, _ = admin(["init", "--member", "chris"], tmp_path, monkeypatch)
    assert code == 0 and out == "token: slc_tok\ninvite: INV1\n"


def test_member_list(tmp_path, monkeypatch):
    code, out, _, _ = admin(["member", "list"], tmp_path, monkeypatch)
    assert code == 0 and out == "chris  chris@example.com  admin\npat  -  member\n"


def test_token_existing_same_role(tmp_path, monkeypatch):
    code, out, _, st = admin(["token", "create", "w_ab12", "admin", "--member", "chris"], tmp_path, monkeypatch)
    assert code == 0 and out == "slc_new\n" and st["tokens"] == [("w_ab12", "admin", "m_c")]


def test_token_existing_other_role(tmp_path, monkeypatch):
    code, out, err, st = admin(["token", "create", "acme", "admin", "--member", "pat"], tmp_path, monkeypatch)
    assert code == 2 and "pat is member, not admin" in err and out == "" and st["tokens"] == []


def test_token_new_needs_email(tmp_path, monkeypatch):
    code, out, err, st = admin(["token", "create", "w_ab12", "member", "--member", "sam"], tmp_path, monkeypatch)
    assert code == 2 and out == "" and "--email" in err and st["tokens"] == []
    code, out, _, st = admin(["token", "create", "w_ab12", "member", "--member", "sam", "--email", "sam@example.com"],
                             tmp_path, monkeypatch)
    assert code == 0 and st["members"]["sam"]["email"] == "sam@example.com"


def test_invite(tmp_path, monkeypatch):
    code, out, _, st = admin(["invite", "pat", "--email", "pat@example.com"], tmp_path, monkeypatch)
    assert code == 0 and out == "invite: INV2\n" and st["email"] == ("m_p", "pat@example.com")
    code, _, err, _ = admin(["invite", "nobody"], tmp_path, monkeypatch)
    assert code == 2


def test_backup_restore_cli(tmp_path, monkeypatch):
    f = str(tmp_path / "b.bak")
    assert admin(["backup", f], tmp_path, monkeypatch)[0] == 0
    code, _, err, _ = admin(["restore", f], tmp_path, monkeypatch, server_running=lambda: True)
    assert code == 2 and "refused" in err
    assert admin(["restore", f], tmp_path, monkeypatch)[0] == 0
