"""CLI protocol probes: bad env/args exit cleanly, secrets never leak, sync offset is exact."""

import io
import json
import os

import pytest

from slcloud import cli

URL = "http://h:8787"
TOK = "slc_" + "7" * 32


class Poster:
    def __init__(self, status=200, body=None, exc=None):
        self.calls, self.status, self.body, self.exc = [], status, body or {}, exc

    def __call__(self, method, url, token, body):
        self.calls.append((method, url, token, body))
        if self.exc:
            raise self.exc
        return self.status, self.body


def run(argv, tmp, post=None, env=None, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    e = {"SLCLOUD_URL": URL, "SLCLOUD_TOKEN": TOK, "HOME": str(tmp),
         "SLCLOUD_OUTBOX": str(tmp / "outbox.jsonl")}
    e.update(env or {})
    e = {k: v for k, v in e.items() if v is not None}
    code = cli.main(argv, http=post or Poster(), env=e, stdin=io.StringIO(stdin), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _row(i):
    return json.dumps({"v": 1, "id": f"r{i}", "signal": "rework", "date": "2026-11-14"}) + "\n"


def _state(tmp):
    p = tmp / ".config/slcloud/sync.json"
    return json.loads(p.read_text()) if p.exists() else None


def _files_text(tmp):
    text = ""
    for root, _, files in os.walk(tmp):
        for f in files:
            if f != "outbox.jsonl":
                text += open(os.path.join(root, f), errors="replace").read()
    return text


@pytest.mark.parametrize("argv", [["connect", "claude"], ["sync"], ["key", "set", "anthropic"]])
@pytest.mark.parametrize("missing", [{"SLCLOUD_URL": None}, {"SLCLOUD_TOKEN": None}, {"SLCLOUD_TOKEN": ""}])
def test_missing_env_clean_exit(tmp_path, argv, missing):
    code, out, err = run(argv, tmp_path, env=missing, stdin="sk-x\n")
    assert code == 1
    assert err == "set SLCLOUD_URL and SLCLOUD_TOKEN\n" and out == ""
    assert "Traceback" not in err


@pytest.mark.parametrize("argv", [
    [], ["connect"], ["connect", "nope"], ["connect", "claude", "x"], ["sync", "x"], ["key"], ["key", "set"],
    ["key", "get", "anthropic"], ["token"], ["token", "create", "w"], ["token", "list", "w", "admin"], ["--help"],
])
def test_bad_args_print_usage_exit_2(tmp_path, argv):
    code, out, err = run(argv, tmp_path)
    assert code == 2, argv
    assert err == cli.USAGE + "\n" and out == "", argv
    assert TOK not in out + err


@pytest.mark.parametrize("exc", [RuntimeError(f"boom Bearer {TOK}"), ConnectionError(TOK), ValueError(TOK)])
def test_sync_http_exception_hides_token_keeps_offset(tmp_path, exc):
    (tmp_path / "outbox.jsonl").write_text(_row(1))
    code, out, err = run(["sync"], tmp_path, Poster(exc=exc))
    assert code == 1 and err == f"error: {type(exc).__name__}\n"
    assert TOK not in out + err
    assert _state(tmp_path) is None
    ok = Poster(200)
    assert run(["sync"], tmp_path, ok)[0] == 0
    assert [r["id"] for r in ok.calls[0][3]["rows"]] == ["r1"]


def test_bad_url_no_traceback_no_token(tmp_path):
    (tmp_path / "outbox.jsonl").write_text(_row(1))
    code, out, err = run(["sync"], tmp_path, cli._http, env={"SLCLOUD_URL": "notaurl"})
    assert code == 1 and err.startswith("error: ") and "Traceback" not in err
    assert TOK not in out + err


def test_sync_offset_exact_across_failure_and_partial_line(tmp_path):
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(_row(1) + _row(2) + '{"v": 1, "id": "r3"')  # r3 still being written
    bad = Poster(503)
    assert run(["sync"], tmp_path, bad)[0] == 1
    assert _state(tmp_path) is None
    ok = Poster(200)
    assert run(["sync"], tmp_path, ok)[0] == 0
    assert [r["id"] for r in ok.calls[0][3]["rows"]] == ["r1", "r2"]
    assert _state(tmp_path) == {URL: len((_row(1) + _row(2)).encode())}
    with open(ob, "a") as f:
        f.write(', "signal": "rework", "date": "2026-11-14"}\n' + _row(4))
    assert run(["sync"], tmp_path, ok)[0] == 0
    assert [r["id"] for r in ok.calls[1][3]["rows"]] == ["r3", "r4"]
    code, out, _ = run(["sync"], tmp_path, ok)
    assert (code, out, len(ok.calls)) == (0, "nothing to sync\n", 2)


def test_sync_state_write_failure_keeps_previous_offsets(tmp_path, monkeypatch):
    ob = tmp_path / "outbox.jsonl"
    ob.write_text(_row(1))
    other = "http://other:8787"
    assert run(["sync"], tmp_path, Poster(200))[0] == 0
    assert run(["sync"], tmp_path, Poster(200), env={"SLCLOUD_URL": other})[0] == 0
    before = _state(tmp_path)
    with open(ob, "a") as f:
        f.write(_row(2))

    real_dump = json.dump

    def torn(obj, fh, **kw):  # write half the document, then die (disk full, crash)
        s = json.dumps(obj)
        fh.write(s[: len(s) // 2])
        raise OSError("disk full")

    monkeypatch.setattr(cli.json, "dump", torn)
    code, _, err = run(["sync"], tmp_path, Poster(200))
    assert code == 1 and err == "error: OSError\n"
    monkeypatch.setattr(cli.json, "dump", real_dump)
    assert _state(tmp_path) == before  # neither URL's offset was lost
    assert not [f for f in os.listdir(tmp_path / ".config/slcloud") if f != "sync.json"]


def test_token_and_key_never_written_or_echoed(tmp_path):
    (tmp_path / "outbox.jsonl").write_text(_row(1))
    outs = ""
    for argv, post, stdin in [(["sync"], Poster(200), ""), (["sync"], Poster(500), ""),
                              (["key", "set", "openai"], Poster(400, {"error": f"bad sk-secret-9999 {TOK}"}),
                               "sk-secret-9999\n"),
                              (["key", "set", "openai"], Poster(200, {"last4": "9999", "works": True}),
                               "sk-secret-9999\n")]:
        _, out, err = run(argv, tmp_path, post, stdin=stdin)
        outs += out + err
    assert TOK not in outs and "sk-secret" not in outs
    written = _files_text(tmp_path)
    assert TOK not in written and "sk-secret" not in written


def test_connect_prints_token_only_on_stdout(tmp_path):
    code, out, err = run(["connect", "claude"], tmp_path)
    assert code == 0 and TOK in out and err == ""
    assert TOK not in _files_text(tmp_path)
