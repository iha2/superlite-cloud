import sqlite3

import pytest

from slcloud.store import Store


@pytest.fixture
def st(tmp_path):
    return Store(str(tmp_path / "s.db"))


def test_tables(st):
    names = {r["name"] for r in st.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"members", "tokens", "tasks", "invites", "web_sessions", "sign_in_misses",
            "notices", "nudges"} <= names


def test_token_needs_member(st):
    ins = "INSERT INTO tokens(id,hash,workspace,role,member,created) VALUES(?,?,?,?,?,?)"
    st.exec(ins, ("k1", "h1", "w", "admin", "m_1", "now"))
    with pytest.raises(sqlite3.IntegrityError):
        st.exec(ins, ("k2", "h2", "w", "admin", None, "now"))


@pytest.mark.parametrize("owner,name", [(None, "x"), ("m_1", None)])
def test_task_needs_owner(st, owner, name):
    with pytest.raises(sqlite3.IntegrityError):
        st.exec("INSERT INTO tasks(id,workspace,status,owner,name) VALUES('t',?,?,?,?)", ("w", "queued", owner, name))
    st.exec("INSERT INTO tasks(id,workspace,status,owner,name) VALUES('t','w','queued','m_1','x')")


def test_email_unique(st):
    ins = "INSERT INTO members(id,workspace,handle,email,role) VALUES(?,?,?,?,?)"
    st.exec(ins, ("m_1", "w", "a", None, "member"))
    st.exec(ins, ("m_2", "w", "b", None, "member"))
    st.exec(ins, ("m_3", "w", "c", "pat@example.com", "member"))
    with pytest.raises(sqlite3.IntegrityError):
        st.exec(ins, ("m_4", "w2", "d", "pat@example.com", "member"))


def test_handle_unique_across_workspaces(st):
    ins = "INSERT INTO members(id,workspace,handle,role) VALUES(?,?,?,?)"
    st.exec(ins, ("m_1", "w1", "pat", "member"))
    with pytest.raises(sqlite3.IntegrityError):
        st.exec(ins, ("m_2", "w2", "pat", "member"))
    with pytest.raises(sqlite3.IntegrityError):
        st.exec(ins, ("m_3", "w1", "pat", "member"))


OLD_TASKS = ("CREATE TABLE tasks(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, repo TEXT, prompt TEXT, agent TEXT, "
             "base TEXT, status TEXT NOT NULL, session TEXT, plan TEXT, answer TEXT, result TEXT, error TEXT, "
             "created TEXT, updated TEXT, owner TEXT NOT NULL, name TEXT NOT NULL, dir TEXT)")


def _old_db(path):
    db = sqlite3.connect(path)
    db.execute(OLD_TASKS)
    db.execute("INSERT INTO tasks(id,workspace,status,owner,name) VALUES('t_old','w','done','m','old')")
    db.commit()
    db.close()


def test_old_db_migrates(tmp_path):
    path = str(tmp_path / "old.db")
    _old_db(path)
    row = Store(path).query("SELECT * FROM tasks")[0]
    assert row["orchestrator"] is None and row["waiting_on"] is None
    from slcloud.engine import Engine
    assert Engine(Store(path)).get("w", "t_old").orchestrator == "ai"


def test_migration_idempotent(tmp_path):
    path = str(tmp_path / "old.db")
    _old_db(path)
    Store(path)
    st = Store(path)
    cols = [r["name"] for r in st.query("PRAGMA table_info(tasks)")]
    assert cols.count("orchestrator") == 1 and cols.count("waiting_on") == 1
