import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slcloud import webauth
from slcloud.config import Settings
from slcloud.store import Store

OK = "https://cloud.example.com"


class FakeAccounts:
    def __init__(self):
        self.calls, self.signed_out = [], []
        self.fail_redeem = False

    def origin_ok(self, origin, public_url):
        self.calls.append("origin_ok")
        return origin == public_url

    def sign_in(self, store, handle, password, now):
        self.calls.append("sign_in")
        return "SID123" if (handle, password) == ("pat", "correct horse battery") else None

    def session_member(self, store, sid, now):
        self.calls.append("session_member")
        return "m_1a2b3c4d" if sid == "SID123" else None

    def invite_info(self, store, code, now):
        self.calls.append("invite_info")
        if code == "X":
            return {"handle": "pat", "email": "pat@example.com"}
        if code == "XSS":
            return {"handle": "<b>pat</b>", "email": "a&b@example.com"}
        return None

    def redeem(self, store, code, password, now):
        self.calls.append("redeem")
        if len(password) < 12:
            raise ValueError("short")
        return "m_1a2b3c4d"

    def sign_out(self, store, sid):
        self.signed_out.append(sid)


def settings(url=OK, tmp="/tmp"):
    return Settings(data_dir=tmp, db_path=f"{tmp}/x.db", work_dir=tmp, master_key="k", public_url=url)


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.exec("INSERT INTO members(id,workspace,handle,email,role,locked) VALUES('m_1a2b3c4d','w_ab12','pat','pat@example.com','member',0)")
    return store


def client(store, url=OK, fake=None):
    fake = fake or FakeAccounts()
    app = FastAPI()
    app.include_router(webauth.make_router(store, settings(url), accounts=fake))
    return TestClient(app, base_url=url, follow_redirects=False), fake


FORM = {"handle": "pat", "password": "correct horse battery"}


def test_cookie_flags(env):
    c, _ = client(env)
    r = c.post("/signin", data=FORM, headers={"Origin": OK})
    assert r.status_code == 303 and r.headers["location"] == "/tokens"
    sc = r.headers["set-cookie"]
    assert "sid=SID123" in sc and "HttpOnly" in sc and "SameSite=strict" in sc.replace("Strict", "strict")
    assert "Max-Age=43200" in sc and "Secure" in sc
    c2, _ = client(env, url="http://localhost:8787")
    sc2 = c2.post("/signin", data=FORM, headers={"Origin": "http://localhost:8787"}).headers["set-cookie"]
    assert "Secure" not in sc2 and "HttpOnly" in sc2


def test_origin_mismatch(env):
    c, fake = client(env)
    assert c.post("/signin", data=FORM, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/signin", data=FORM).status_code == 403
    assert "sign_in" not in fake.calls


def test_same_error_bad_handle_and_password(env):
    c, _ = client(env)
    a = c.post("/signin", data={"handle": "ghost", "password": "x"}, headers={"Origin": OK})
    b = c.post("/signin", data={"handle": "pat", "password": "x"}, headers={"Origin": OK})
    assert a.status_code == b.status_code == 401 and a.text == b.text
    assert "set-cookie" not in a.headers


def test_register_shows_handle_email(env):
    c, _ = client(env)
    t = c.get("/register?code=X").text
    assert "pat" in t and "pat@example.com" in t
    x = c.get("/register?code=XSS").text
    assert "<b>pat</b>" not in x and "&lt;b&gt;pat&lt;/b&gt;" in x
    assert c.get("/register?code=nope").status_code == 404


def test_register_post(env):
    c, fake = client(env)
    r = c.post("/register", data={"code": "X", "password": "correct horse battery"}, headers={"Origin": OK})
    assert r.status_code == 303 and "sid=SID123" in r.headers["set-cookie"]
    short = c.post("/register", data={"code": "X", "password": "short"}, headers={"Origin": OK})
    assert short.status_code == 400 and "12 characters" in short.text
    assert c.post("/register", data={"code": "X", "password": "p" * 12}).status_code == 403


def test_no_cookie_none(env):
    fake = FakeAccounts()
    who = webauth.session_principal(env, settings(), accounts=fake)
    app = FastAPI()

    @app.get("/me")
    def me(request: __import__("fastapi").Request):
        p = who(request)
        return None if p is None else {"w": p.workspace, "role": p.role, "m": p.member, "h": p.handle}

    c = TestClient(app)
    assert c.get("/me").json() is None
    assert fake.calls == []
    c.cookies.set("sid", "bad")
    assert c.get("/me").json() is None
    c.cookies.set("sid", "SID123")
    assert c.get("/me").json() == {"w": "w_ab12", "role": "member", "m": "m_1a2b3c4d", "h": "pat"}
    env.exec("UPDATE members SET locked=1")
    assert c.get("/me").json() is None


def test_signout_clears(env):
    c, fake = client(env)
    c.cookies.set("sid", "SID123")
    r = c.post("/signout", headers={"Origin": OK})
    assert fake.signed_out == ["SID123"]
    assert r.status_code == 303
    sc = r.headers["set-cookie"]
    assert "sid=" in sc and ("Max-Age=0" in sc or "expires" in sc.lower())
    assert c.post("/signout").status_code == 403
