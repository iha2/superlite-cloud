from slcloud.store import Store
from slcloud import filestore

PLAN = "docs/plans/2026-11-24-login-fix.review.md"
BR = "superlite/sessions/login-fix"


def mk(tmp_path):
    return Store(str(tmp_path / "s.db"))


def test_publish_idempotent(tmp_path):
    s = mk(tmp_path)
    assert filestore.request_publish(s, "t_1") == "pending"
    assert filestore.request_publish(s, "t_1") == "pending"
    assert s.query("SELECT COUNT(*) AS n FROM task_files")[0]["n"] == 1
    assert filestore.pending(s) == ["t_1"]
    filestore.set_gate(s, "t_1", BR, "9f2c1a7e", True, [PLAN])
    assert filestore.pending(s) == []
    assert filestore.request_publish(s, "t_1") == "ready"


def test_gate_keeps_plans_after_publish(tmp_path):
    s = mk(tmp_path)
    filestore.set_gate_sha(s, "t_1", "9f2c1a7e", [PLAN])
    assert filestore.get(s, "t_1")["state"] == "gated"
    assert filestore.request_publish(s, "t_1") == "pending"
    filestore.set_gate(s, "t_1", BR, "9f2c1a7e", True, [PLAN])
    assert filestore.get(s, "t_1") == {
        "task_id": "t_1", "state": "ready", "branch": BR, "sha": "9f2c1a7e",
        "pushed": True, "bundle": "", "plans": [PLAN], "edits": None}
    filestore.set_gate(s, "t_1", BR, "9f2c1a7e", False, [], bundle="/b.bundle")
    g = filestore.get(s, "t_1")
    assert g["plans"] == [PLAN] and g["pushed"] is False and g["bundle"] == "/b.bundle"


def test_edits_replace(tmp_path):
    s = mk(tmp_path)
    filestore.set_gate(s, "t_1", BR, "9f2c1a7e", True, [PLAN])
    filestore.set_edits(s, "t_1", "ab12cd34", ["README.md", PLAN])
    assert filestore.get(s, "t_1")["edits"] == {"sha": "ab12cd34", "files": ["README.md", PLAN]}
    filestore.set_edits(s, "t_1", "ee55", ["README.md"])
    assert filestore.get(s, "t_1")["edits"] == {"sha": "ee55", "files": ["README.md"]}


def test_clear(tmp_path):
    s = mk(tmp_path)
    filestore.request_publish(s, "t_1")
    filestore.clear(s, "t_1")
    assert filestore.get(s, "t_1") is None
    assert filestore.pending(s) == []
    assert filestore.get(s, "t_9") is None


def test_pending_oldest_first(tmp_path):
    s = mk(tmp_path)
    filestore.request_publish(s, "t_2")
    filestore.request_publish(s, "t_1")
    assert filestore.pending(s) == ["t_2", "t_1"]


def test_schema_has_task_files(tmp_path):
    s = mk(tmp_path)
    cols = {r["name"] for r in s.query("PRAGMA table_info(task_files)")}
    assert {"task_id", "state", "branch", "sha", "pushed", "bundle", "plans",
            "edits_sha", "edits_files", "updated"} <= cols
