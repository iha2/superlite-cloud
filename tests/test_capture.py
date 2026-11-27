import pytest

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


def test_rapid_text_events_all_arrive_in_order():
    c, ev, now = make()
    c.feed({"kind": "text", "text": "first message"})
    now[0] = 0.01
    c.feed({"kind": "text", "text": "second message"})
    now[0] = 0.02
    c.feed({"kind": "text", "text": "z" * 500})
    c.flush()
    texts = [e[2]["text"] for e in ev]
    assert texts == ["first message", "second message", "z" * 500]
    assert [e[2]["step"] for e in ev] == [1, 2, 3]
    c.flush()
    assert len(ev) == 3


def test_lone_text_event_is_emitted_without_flush():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "hello"})
    assert [e[2]["text"] for e in ev] == ["hello"]


def test_long_message_arrives_whole():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "x" * 3000})
    c.flush()
    assert len(ev) == 1 and len(ev[0][2]["text"]) == 3000


def test_truncate():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "x" * 5000})
    c.flush()
    assert len(ev[0][2]["text"]) == 4000


def test_thinking_event_is_masked_capped_and_separate():
    c, ev, _ = make(["zzsecretvalue123"])
    c.feed({"kind": "thinking", "text": "pondering zzsecretvalue123 sk-abcdefghijklmnop1234 ok"})
    c.feed({"kind": "text", "text": "answer"})
    c.feed({"kind": "thinking", "text": "t" * 5000})
    c.flush()
    assert [e[1] for e in ev] == ["thinking", "text", "thinking"]
    assert ev[0][2]["step"] == 1 and ev[0][2]["text"].startswith("pondering ")
    assert "zzsecretvalue123" not in ev[0][2]["text"] and "sk-" not in ev[0][2]["text"]
    assert ev[1][2] == {"step": 2, "text": "answer"}
    assert len(ev[2][2]["text"]) == 4000


def test_split_secret_in_thinking_masked_and_kept_apart_from_text():
    c, ev, _ = make(["zzsecretvalue123"])
    c.feed({"kind": "thinking", "text": "key zzsecret"})
    c.feed({"kind": "thinking", "text": "value123 end"})
    c.feed({"kind": "text", "text": "hello"})
    c.flush()
    assert all("zzsecret" not in str(e) and "value123" not in str(e) for e in ev)
    assert [e[1] for e in ev] == ["thinking", "text"] and ev[1][2]["text"] == "hello"


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


def test_secret_split_across_two_chunks_masked_with_rapid_events():
    c, ev, now = make(["zzsecretvalue123"])
    c.feed({"kind": "text", "text": "key zzsecret"})
    now[0] = 0.01
    c.feed({"kind": "text", "text": "value123 here"})
    c.flush()
    out = " ".join(e[2]["text"] for e in ev)
    assert "zzsecret" not in out and "value123" not in out and "here" in out


@pytest.mark.parametrize("kind", ["text", "thinking"])
def test_secret_split_across_three_events_is_masked(kind):
    secret = "zzsecretvalue123"
    c, ev, _ = make([secret])
    c.feed({"kind": kind, "text": "key zzsecret"})
    c.feed({"kind": kind, "text": "value"})
    assert ev == []
    c.feed({"kind": kind, "text": "123 end"})
    c.flush()
    texts = [e[2]["text"] for e in ev]
    assert [e[1] for e in ev] == [kind]
    assert secret not in "".join(texts)
    assert all(secret not in text for text in texts)
    assert texts == ["key ***e123 end"]


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


def test_suspect_tail_block_not_glued_to_next_text_event():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "Plan A"})
    c.feed({"kind": "text", "text": "Next step"})
    c.flush()
    assert [e[2]["text"] for e in ev] == ["Plan A", "Next step"]


def test_suspect_tail_block_emitted_whole_before_tool_event():
    c, ev, _ = make()
    c.feed({"kind": "text", "text": "Plan A"})
    c.feed({"kind": "tool", "tool": "Edit", "target": "api.py"})
    assert [(e[1], e[2].get("text")) for e in ev] == [("text", "Plan A"), ("tool", None)]
