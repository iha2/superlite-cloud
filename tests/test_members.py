import re

import pytest

from slcloud import members
from slcloud.store import Store


@pytest.fixture
def st(tmp_path):
    return Store(str(tmp_path / "s.db"))


def test_create_and_lookup(st):
    mid = members.create_member(st, "w_ab12", "pat", "pat@example.com", "member")
    assert re.fullmatch(r"m_[0-9a-f]{8}", mid)
    want = {"id": mid, "workspace": "w_ab12", "handle": "pat", "email": "pat@example.com",
            "role": "member", "locked": 0}
    assert members.member_by_email(st, "PAT@Example.com") == want
    assert members.member_by_handle(st, "w_ab12", "pat") == want
    assert members.member_by_handle(st, "w_other", "pat") is None
    assert members.member_by_email(st, "nobody@example.com") is None


def test_bad_handle(st):
    for h in ("Pat!", "", "a b", "PAT"):
        with pytest.raises(ValueError):
            members.create_member(st, "w", h, None, "member")


def test_email_case(st):
    members.create_member(st, "w", "pat", "Pat@Example.COM", "member")
    assert st.query("SELECT email FROM members")[0]["email"] == "pat@example.com"


def test_email_unique_server_wide(st):
    members.create_member(st, "w1", "pat", "pat@example.com", "member")
    with pytest.raises(ValueError):
        members.create_member(st, "w2", "pat2", "PAT@example.com", "member")
    members.create_member(st, "w1", "a", None, "member")
    members.create_member(st, "w1", "b", None, "member")


def test_handle_unique(st):
    members.create_member(st, "w", "pat", None, "member")
    with pytest.raises(ValueError):
        members.create_member(st, "w", "pat", None, "member")
    with pytest.raises(ValueError, match="handle taken"):
        members.create_member(st, "w2", "pat", None, "member")


def test_list(st):
    c = members.create_member(st, "w", "chris", "chris@example.com", "admin")
    members.create_member(st, "x", "other", None, "member")
    assert members.list_members(st, "w") == [
        {"id": c, "handle": "chris", "email": "chris@example.com", "role": "admin", "locked": 0}]


def test_rename_and_email(st):
    a = members.create_member(st, "w", "pat", None, "member")
    members.create_member(st, "w", "chris", "chris@example.com", "member")
    members.rename(st, a, "pat-2")
    assert members.member_by_handle(st, "w", "pat-2")["id"] == a
    with pytest.raises(ValueError):
        members.rename(st, a, "chris")
    with pytest.raises(ValueError):
        members.rename(st, a, "Bad!")
    members.set_email(st, a, "Pat@Example.com")
    assert members.member_by_email(st, "pat@example.com")["id"] == a
    with pytest.raises(ValueError):
        members.set_email(st, a, "chris@example.com")


def test_set_role_tokens(st):
    a = members.create_member(st, "w", "pat", None, "member")
    b = members.create_member(st, "w", "chris", None, "member")
    ins = "INSERT INTO tokens(id,hash,workspace,role,member,created) VALUES(?,?,?,?,?,'n')"
    st.exec(ins, ("k1", "h1", "w", "member", a))
    st.exec(ins, ("k2", "h2", "w", "member", a))
    st.exec(ins, ("k3", "h3", "w", "member", b))
    members.set_role(st, a, "admin")
    roles = {r["id"]: r["role"] for r in st.query("SELECT id, role FROM tokens")}
    assert roles == {"k1": "admin", "k2": "admin", "k3": "member"}
    assert members.list_members(st, "w")[1]["role"] == "admin"
    with pytest.raises(ValueError):
        members.set_role(st, a, "root")


def test_lock(st):
    a = members.create_member(st, "w", "pat", None, "member")
    b = members.create_member(st, "w", "chris", None, "member")
    st.exec("UPDATE members SET password='x' WHERE id=?", (a,))
    st.exec("INSERT INTO tokens(id,hash,workspace,role,member,created) VALUES('k1','h1','w','member',?,'n')", (a,))
    st.exec("INSERT INTO tokens(id,hash,workspace,role,member,created) VALUES('k2','h2','w','member',?,'n')", (b,))
    st.exec("INSERT INTO web_sessions(sid_hash,member,expires) VALUES('s1',?,1)", (a,))
    st.exec("INSERT INTO web_sessions(sid_hash,member,expires) VALUES('s2',?,1)", (b,))
    st.exec("INSERT INTO tasks(id,workspace,status,owner,name) VALUES('t_1','w','done',?,'x')", (a,))
    members.lock(st, a)
    assert [r["id"] for r in st.query("SELECT id FROM tokens")] == ["k2"]
    assert [r["sid_hash"] for r in st.query("SELECT sid_hash FROM web_sessions")] == ["s2"]
    row = st.query("SELECT password, locked FROM members WHERE id=?", (a,))[0]
    assert row == {"password": None, "locked": 1}
    assert len(st.query("SELECT id FROM tasks")) == 1
