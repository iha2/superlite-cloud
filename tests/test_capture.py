from slcloud.capture import Capture


def make(secrets=(), t=None):
    ev, now = [], t or [0.0]
    return Capture("t_1", lambda *a: ev.append(a), list(secrets), lambda: now[0]), ev, now


def test_tool_event():
    c, ev, _ = make()
    c.feed({"kind": "tool", "tool": "Edit", "target": "api.py"})
    assert ev == [("t_1", "tool", {"step": 1, "tool": "Edit", "target": "api.py"})]


def test_redacts_key():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "using sk-abcdefghijklmnop1234 now"})
    c.flush()
    assert ev[0][2] == {"step": 1, "text": "using [redacted] now"}


def test_fake_key_masked():
    c, ev, _ = make(["zzsecretvalue123"])
    c.feed({"kind": "text", "text": "key zzsecretvalue123 here"})
    c.feed({"kind": "tool", "tool": "Bash", "target": "echo zzsecretvalue123"})
    c.flush()
    assert all("zzsecretvalue123" not in str(e) for e in ev) and len(ev) == 2


def test_throttle():
    c, ev, now = make()
    for i, txt in enumerate(["a", "b", "c"]):
        now[0] = i * 0.1
        c.feed({"kind": "text", "text": txt})
    assert ev == []
    c.flush()
    assert len(ev) == 1 and ev[0][2]["text"] == "c"
    c.flush()
    assert len(ev) == 1


def test_throttle_gap_elapsed():
    c, ev, now = make()
    c.feed({"kind": "text", "text": "a"})
    now[0] = 0.3
    c.feed({"kind": "text", "text": "b"})
    assert [e[2]["text"] for e in ev] == ["b"]


def test_truncate():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "x" * 500})
    c.flush()
    assert len(ev[0][2]["text"]) == 200


def test_status_never_throttled():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "hi"})
    c.feed({"kind": "status", "status": "running"})
    c.feed({"kind": "file", "path": "a.py"})
    c.feed({"kind": "status", "status": "done"})
    assert [e[1] for e in ev] == ["text", "status", "file", "status"]
    assert [e[2]["step"] for e in ev] == [1, 2, 3, 4]


def test_strips_sandbox_prefix():
    c, ev, _ = make()
    c.feed({"kind": "tool", "tool": "Edit", "target": "/work/api2.py"})
    c.feed({"kind": "file", "path": "/work/src/a.py"})
    assert ev[0][2]["target"] == "api2.py" and ev[1][2]["path"] == "src/a.py"


def _stream(chunks, secrets=()):
    c, ev, now = make(secrets)
    for ch in chunks:
        now[0] += 1.0  # every chunk is past the throttle gap, so each one emits on its own
        c.feed({"kind": "text", "text": ch})
    c.flush()
    return " ".join(e[2]["text"] for e in ev)


def test_provider_key_split_across_chunks_is_redacted():
    out = _stream(["using sk-abcdefgh", "ijklmnop1234 now"])
    assert "sk-" not in out and "abcdefgh" not in out and "ijklmnop" not in out
    assert "now" in out


def test_key_split_inside_marker_and_flushed_tail():
    assert "sk" not in _stream(["token s", "k-abcdefghijklmnop1234"]).replace("task", "")
    out = _stream(["see key-abcdefghijklmnop", "12345"])
    assert "12345" not in out and "abcdefgh" not in out


def test_configured_secret_split_across_chunks_is_masked():
    out = _stream(["key zzsecret", "value123 here"], ["zzsecretvalue123"])
    assert "zzsecret" not in out and "value123" not in out and "here" in out


def test_plain_text_split_across_chunks_still_flows():
    assert "world" in _stream(["hello ", "world"])
