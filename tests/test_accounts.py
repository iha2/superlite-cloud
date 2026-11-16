import hashlib

import pytest

from slcloud import accounts
from slcloud.store import Store

PW = "correct horse battery"
T0 = 1_000_000.0

def mk(path):
    return Store(str(path))


@pytest.fixture
def store(tmp_path):
    s = mk(tmp_path / "s.db")
    s.exec("INSERT INTO members(id,workspace,handle,email,role,locked) VALUES('m_1a2b3c4d','w1','pat','pat@example.com','member',0)")
    return s


def signed_up(store):
    code = accounts.create_invite(store, "m_1a2b3c4d", T0)
    accounts.redeem(store, code, PW, T0)
    return code


def test_hash_format():
    h = accounts.hash_password(PW)
    p = h.split("$")
    assert p[:4] == ["scrypt", "15", "8", "1"]
    assert len(p[4]) == 32 and len(p[5]) == 128
    assert accounts.hash_password(PW) != h


def test_short_password(store):
    with pytest.raises(ValueError):
        accounts.hash_password("a" * 11)
    code = accounts.create_invite(store, "m_1a2b3c4d", T0)
    with pytest.raises(ValueError):
        accounts.redeem(store, code, "a" * 11, T0)
    assert accounts.invite_info(store, code, T0) is not None  # not burned


def test_invite_info_and_redeem(store):
    code = accounts.create_invite(store, "m_1a2b3c4d", T0)
    assert accounts.invite_info(store, code, T0) == {"handle": "pat", "email": "pat@example.com"}
    assert accounts.redeem(store, code, PW, T0) == "m_1a2b3c4d"
    assert accounts.invite_info(store, code, T0) is None
    row = store.query("SELECT code_hash FROM invites")[0]
    assert row["code_hash"] == hashlib.sha256(code.encode()).hexdigest()


def test_invite_single_use_7_days(store):
    code = accounts.create_invite(store, "m_1a2b3c4d", T0)
    accounts.redeem(store, code, PW, T0)
    with pytest.raises(ValueError):
        accounts.redeem(store, code, PW, T0)
    c2 = accounts.create_invite(store, "m_1a2b3c4d", T0)
    with pytest.raises(ValueError):
        accounts.redeem(store, c2, PW, T0 + 7 * 86400 + 1)
    assert accounts.invite_info(store, c2, T0 + 7 * 86400 + 1) is None
    assert accounts.redeem(store, c2, PW, T0 + 7 * 86400) == "m_1a2b3c4d"


def test_redeem_unlocks(store):
    store.exec("UPDATE members SET locked=1")
    signed_up(store)
    assert store.query("SELECT locked FROM members")[0]["locked"] == 0


def test_sign_in_and_session(store):
    signed_up(store)
    assert accounts.sign_in(store, "pat", "wrong", T0) is None
    sid = accounts.sign_in(store, "pat", PW, T0)
    assert len(sid) == 43
    assert accounts.session_member(store, sid, T0 + 10) == "m_1a2b3c4d"
    assert accounts.session_member(store, sid, T0 + 12 * 3600 + 1) is None
    sid2 = accounts.sign_in(store, "pat", PW, T0)
    accounts.sign_out(store, sid2)
    assert accounts.session_member(store, sid2, T0) is None
    assert accounts.session_member(store, "nope", T0) is None


def test_sid_hash_only(store):
    signed_up(store)
    sid = accounts.sign_in(store, "pat", PW, T0)
    rows = store.query("SELECT * FROM web_sessions")
    assert rows[0]["sid_hash"] == hashlib.sha256(sid.encode()).hexdigest()
    assert sid not in str(rows)


def test_five_miss_wait_survives_restart(tmp_path):
    s = mk(tmp_path / "r.db")
    s.exec("INSERT INTO members(id,workspace,handle,role,locked) VALUES('m_1a2b3c4d','w1','pat','member',0)")
    signed_up(s)
    for _ in range(5):
        assert accounts.sign_in(s, "pat", "bad password!!", T0) is None
    s2 = Store(str(tmp_path / "r.db"))
    assert accounts.sign_in(s2, "pat", PW, T0 + 10) is None
    assert accounts.sign_in(s2, "pat", PW, T0 + 899) is None
    assert accounts.sign_in(s2, "pat", PW, T0 + 901) is not None
    assert s2.query("SELECT * FROM sign_in_misses") == [] or s2.query("SELECT count FROM sign_in_misses")[0]["count"] == 0


def test_right_password_clears_count(store):
    signed_up(store)
    for _ in range(4):
        accounts.sign_in(store, "pat", "bad password!!", T0)
    assert accounts.sign_in(store, "pat", PW, T0) is not None
    for _ in range(4):
        accounts.sign_in(store, "pat", "bad password!!", T0)
    assert accounts.sign_in(store, "pat", PW, T0) is not None


def test_same_message_and_dummy(store, monkeypatch):
    signed_up(store)
    store.exec("INSERT INTO members(id,workspace,handle,role,locked,password) VALUES('m_dead0001','w1','lck','member',1,?)", (accounts.hash_password(PW),))
    calls = []

    def fake(pw, stored):
        calls.append(stored)
        return False

    monkeypatch.setattr(accounts, "verify", fake)
    assert accounts.sign_in(store, "ghost", PW, T0) is None
    assert calls == [accounts.DUMMY_HASH]
    calls.clear()
    assert accounts.sign_in(store, "lck", PW, T0) is None
    assert len(calls) == 1
    calls.clear()
    assert accounts.sign_in(store, "pat", "wrong", T0) is None
    assert len(calls) == 1 and calls[0] != accounts.DUMMY_HASH


def test_origin_ok():
    u = "https://cloud.example.com"
    assert accounts.origin_ok("https://cloud.example.com", u)
    assert not accounts.origin_ok("https://evil.example.com", u)
    assert not accounts.origin_ok("http://cloud.example.com", u)
    assert not accounts.origin_ok(None, u)
    assert not accounts.origin_ok("null", u)


def test_check_public_url():
    c = accounts.check_public_url
    assert c("http://localhost:8787", "127.0.0.1:8787") == "Open http://localhost:8787 instead"
    assert c("http://localhost:8787", "localhost:8787") is None
    assert c("http://example.com", "example.com") == "refused: plain http"
    assert c("https://cloud.example.com", "cloud.example.com") is None
    assert c("https://cloud.example.com", "10.0.0.1") == "Open https://cloud.example.com instead"


def test_concurrent_wrong_passwords_count_every_miss(store, monkeypatch):
    import threading
    signed_up(store)
    monkeypatch.setattr(accounts, "verify", lambda pw, stored: pw == PW)  # fast; the race is in the count
    results = []

    def go():
        results.append(accounts.sign_in(store, "pat", "wrong password!", T0))

    ts = [threading.Thread(target=go) for _ in range(accounts.MAX_MISSES)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results == [None] * accounts.MAX_MISSES
    assert accounts.sign_in(store, "pat", PW, T0 + 1) is None  # locked out: every miss was counted
