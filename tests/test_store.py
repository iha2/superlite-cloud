import sqlite3

import pytest

from slcloud.config import Settings, load_settings
from slcloud.store import Store


def test_defaults():
    s = load_settings({"SLCLOUD_DATA": "/d", "SLCLOUD_MASTER_KEY": "k"})
    assert s == Settings(
        data_dir="/d", db_path="/d/slcloud.db", work_dir="/d/work", master_key="k",
        public_url="http://localhost:8787", sandbox="docker",
        worker_image="slcloud-worker:0.1.0", gvisor=False,
    )


def test_missing_master_key():
    with pytest.raises(ValueError):
        load_settings({})


def test_gvisor_flag():
    assert load_settings({"SLCLOUD_MASTER_KEY": "k", "SLCLOUD_GVISOR": "1"}).gvisor is True


def test_schema_tables():
    rows = Store(":memory:").query("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert {"workspaces", "tokens", "keys", "tasks", "events", "learn_rows", "lessons", "webhooks"} <= names


def test_exec_returns_rowcount():
    s = Store(":memory:")
    assert s.exec("INSERT INTO workspaces(id,name,created) VALUES(?,?,?)", ("w1", "team", "2026-11-14")) == 1
    assert s.query("SELECT id FROM workspaces") == [{"id": "w1"}]


def test_learn_unique():
    s = Store(":memory:")
    q = "INSERT INTO learn_rows(workspace,member,rid,signal,row,at) VALUES(?,?,?,?,?,?)"
    s.exec(q, ("w", "m", "r", "s", "{}", "t"))
    with pytest.raises(sqlite3.IntegrityError):
        s.exec(q, ("w", "m", "r", "s", "{}", "t"))
