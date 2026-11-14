import json

import pytest

from slcloud import learning
from slcloud.errors import NotFound, Rejected
from slcloud.store import Store

ROW = {"v": 1, "id": "a", "signal": "rework", "date": "2026-11-14"}


@pytest.fixture
def s():
    return Store(":memory:")


def test_ingest_and_duplicate(s):
    assert learning.ingest(s, "w1", "m1", [ROW]) == {"accepted": 1, "duplicate": 0}
    assert learning.ingest(s, "w1", "m1", [ROW]) == {"accepted": 0, "duplicate": 1}


def test_totals(s):
    learning.ingest(s, "w1", "m1", [ROW])
    assert learning.totals(s, "w1") == {"rework": 1}
    assert learning.totals(s, "w2") == {}


def test_extra_field_rejected(s):
    with pytest.raises(Rejected, match="file"):
        learning.accept_row({**ROW, "file": "/etc/passwd"})


def test_whole_batch_rejected(s):
    with pytest.raises(Rejected):
        learning.ingest(s, "w1", "m1", [ROW, {"v": 1, "id": "b", "file": "x"}])
    assert learning.totals(s, "w1") == {}


def test_missing_id_rejected():
    with pytest.raises(Rejected, match="id"):
        learning.accept_row({"v": 1, "signal": "rework"})


def test_propose_approve_lessons(s):
    p = learning.propose(s, "w1", "m1", "run lint first")
    assert p["status"] == "held" and p["proposed_by"] == "m1"
    assert p["id"].startswith("l_") and len(p["id"]) == 10
    assert learning.lessons(s, "w1") == []
    a = learning.approve(s, "w1", p["id"], "m2")
    assert a == {"id": p["id"], "status": "approved", "approved_by": "m2"}
    assert learning.lessons(s, "w1") == [
        {"id": p["id"], "text": "run lint first", "proposed_by": "m1", "approved_by": "m2"}
    ]


def test_approve_unknown_raises(s):
    with pytest.raises(NotFound):
        learning.approve(s, "w1", "l_nope", "m2")


def test_withdrawn_hidden(s):
    p = learning.propose(s, "w1", "m1", "x")
    learning.approve(s, "w1", p["id"], "m2")
    assert learning.withdraw(s, "w1", p["id"]) == {"id": p["id"], "status": "withdrawn"}
    assert learning.lessons(s, "w1") == []
    with pytest.raises(NotFound):
        learning.withdraw(s, "w1", "l_nope")


def test_lessons_other_workspace_empty(s):
    p = learning.propose(s, "w1", "m1", "x")
    learning.approve(s, "w1", p["id"], "m2")
    assert learning.lessons(s, "w2") == []
    with pytest.raises(NotFound):
        learning.approve(s, "w2", p["id"], "m2")


def test_outbox_offset(tmp_path):
    f = tmp_path / "o.jsonl"
    f.write_text(json.dumps(ROW) + "\n")
    rows, off = learning.outbox_rows(str(f), 0)
    assert rows == [ROW] and off == f.stat().st_size
    assert learning.outbox_rows(str(f), off) == ([], off)
    row2 = {**ROW, "id": "b"}
    with open(f, "a") as fh:
        fh.write(json.dumps(row2) + "\n")
    rows, off2 = learning.outbox_rows(str(f), off)
    assert rows == [row2] and off2 == f.stat().st_size


def test_partial_last_line_waits(tmp_path):
    f = tmp_path / "o.jsonl"
    first = json.dumps(ROW) + "\n"
    f.write_text(first + '{"v": 1, "id"')
    rows, off = learning.outbox_rows(str(f), 0)
    assert rows == [ROW] and off == len(first.encode())


def test_outbox_missing_file(tmp_path):
    assert learning.outbox_rows(str(tmp_path / "none"), 0) == ([], 0)


def test_allow_list_is_superlites():
    from superlite import learn_signals

    assert learning.ROW_FIELDS is learn_signals.ROW_FIELDS
    with pytest.raises(Rejected):
        learning.accept_row({"v": 1, "id": "a", "path": "/x"})
