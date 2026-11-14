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


def test_unknown_command():
    code, _, _ = run(["bogus"])
    assert code == 2
    assert run([])[0] == 2
