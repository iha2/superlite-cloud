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
