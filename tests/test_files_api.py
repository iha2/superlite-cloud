import pytest
from fastapi.testclient import TestClient

from slcloud import files, filestore, members
from slcloud.api import create_app
from slcloud.auth import Principal
from slcloud.config import Settings
from slcloud.engine import Engine
from slcloud.store import Store

PLAN = "docs/plans/2026-11-24-login-fix.review.md"
GATE = "9f2c1a7e" + "0" * 32
NEW = "ab12cd34" + "0" * 32
ALL = ["README.md", PLAN]


@pytest.fixture
def world(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "s.db"))
    eng = Engine(st)
    pat = members.create_member(st, "w_ab12", "pat", "pat@example.com", "member")
    chris = members.create_member(st, "w_ab12", "chris", "chris@example.com", "member")
    who = {"p": Principal("w_ab12", "member", pat, "pat"), "c": Principal("w_ab12", "member", chris, "chris")}
    cur = {"who": who["p"]}
    calls = {}

    monkeypatch.setattr(files, "list_files", lambda cache, sha: ALL)

    def make_bundle(cache, tid, out):
        with open(out, "wb") as f:
            f.write(b"BUNDLE")
        return out
    monkeypatch.setattr(files, "make_bundle", make_bundle)

    def accept_remote(cache, tid, gate, sha, precheck=None, record=None):
        calls["remote"] = sha
        if sha == GATE:
            raise files.NotFastForward("no new commits")
        if precheck:
            precheck()
        if record:
            record(sha, [PLAN])
        return sha, [PLAN]
    monkeypatch.setattr(files, "accept_remote", accept_remote)

    def accept_bundle(cache, tid, gate, path, precheck=None, record=None):
        with open(path, "rb") as f:
            calls["bundle"] = f.read()
        if precheck:
            precheck()
        if record:
            record(NEW, [PLAN])
        return NEW, [PLAN]
    monkeypatch.setattr(files, "accept_bundle", accept_bundle)

    task = eng.submit("w_ab12", "https://github.com/acme/app", "fix login", "claude", "main",
                      owner=pat, name="login_fix")
    settings = Settings(data_dir=str(tmp_path), db_path=str(tmp_path / "s.db"),
                        work_dir=str(tmp_path / "work"), master_key="k")
    app = create_app(settings, st, eng, lambda: cur["who"])
    return dict(st=st, eng=eng, task=task, c=TestClient(app), cur=cur, who=who, calls=calls)


def wait(w):
    w["eng"].transition(w["task"].id, "running")
    w["eng"].transition(w["task"].id, "waiting")


def ready(w):
    wait(w)
    filestore.set_gate(w["st"], w["task"].id, "superlite/sessions/login-fix", GATE, True, [PLAN])


def test_not_waiting_409(world):
    w = world
    for status in ("queued", "cancelled"):
        if status == "cancelled":
            w["eng"].transition(w["task"].id, "cancelled")
        r = w["c"].post("/api/tasks/login_fix/publish")
        assert r.status_code == 409 and r.json() == {"error": f"run is {status}, not waiting"}
        assert w["c"].get("/api/tasks/login_fix/files").status_code == 409
        assert w["c"].post("/api/tasks/login_fix/files", json={"sha": NEW}).status_code == 409


def test_other_member_404(world):
    w = world
    ready(w)
    w["cur"]["who"] = w["who"]["c"]
    tid = w["task"].id
    for r in (w["c"].post("/api/tasks/login_fix/publish"), w["c"].get("/api/tasks/pat/login_fix/files"),
              w["c"].get(f"/api/tasks/{tid}/files/bundle"),
              w["c"].post(f"/api/tasks/{tid}/files", json={"sha": NEW})):
        assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_never_published_409(world):
    w = world
    wait(w)
    assert w["c"].get("/api/tasks/login_fix/files").status_code == 409
    assert w["c"].get("/api/tasks/login_fix/files/bundle").status_code == 409
    assert w["c"].post("/api/tasks/login_fix/files", json={"sha": NEW}).status_code == 409


def test_publish_202_then_ready(world):
    w = world
    wait(w)
    r = w["c"].post("/api/tasks/login_fix/publish")
    assert r.status_code == 202 and r.json() == {"state": "pending"}
    assert w["c"].get("/api/tasks/login_fix/files").json() == {"state": "pending"}
    filestore.set_gate(w["st"], w["task"].id, "superlite/sessions/login-fix", GATE, True, [PLAN])
    r = w["c"].post("/api/tasks/login_fix/publish")
    assert r.status_code == 200
    assert r.json() == {"state": "ready", "branch": "superlite/sessions/login-fix", "sha": GATE,
                        "pushed": True, "via": "remote", "files": ALL, "plans": [PLAN], "edits": None}


def test_routes_before_greedy(world):
    w = world
    ready(w)
    r = w["c"].get("/api/tasks/login_fix/files")
    assert r.status_code == 200 and r.json()["state"] == "ready"
    r = w["c"].get("/api/tasks/login_fix/files/bundle")
    assert r.status_code == 200 and r.content == b"BUNDLE"


def test_bundle_download_and_upload(world):
    w = world
    ready(w)
    r = w["c"].get("/api/tasks/login_fix/files/bundle")
    assert r.headers["content-type"] == "application/octet-stream"
    r = w["c"].post("/api/tasks/login_fix/files", json={"sha": NEW})
    assert r.status_code == 200 and r.json()["edits"] == {"sha": NEW, "files": [PLAN]}
    r = w["c"].post(f"/api/tasks/login_fix/files?base={GATE}", content=b"PACK",
                    headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200 and w["calls"]["bundle"] == b"PACK"
    assert filestore.get(w["st"], w["task"].id)["edits"]["sha"] == NEW


def test_rewrite_409(world):
    w = world
    ready(w)
    r = w["c"].post("/api/tasks/login_fix/files", json={"sha": GATE})
    assert r.status_code == 409 and r.json() == {"error": "no new commits"}
    assert filestore.get(w["st"], w["task"].id)["edits"] is None


def test_base_mismatch_409(world):
    w = world
    ready(w)
    r = w["c"].post("/api/tasks/login_fix/files?base=deadbeef", content=b"PACK",
                    headers={"content-type": "application/octet-stream"})
    assert r.status_code == 409 and "bundle" not in w["calls"]
    assert filestore.get(w["st"], w["task"].id)["edits"] is None


def test_empty_body_400(world):
    w = world
    ready(w)
    r = w["c"].post("/api/tasks/login_fix/files", content=b"",
                    headers={"content-type": "application/octet-stream"})
    assert r.status_code == 400
    assert w["c"].post("/api/tasks/login_fix/files", json={}).status_code == 400


def _approve_during_write(w, monkeypatch, name):
    """Simulate an approve landing after resolve() but before the locked write."""
    real = getattr(files, name)

    def racing(*a, **kw):
        w["eng"].transition(w["task"].id, "queued")
        return real(*a, **kw)
    monkeypatch.setattr(files, name, racing)


def test_upload_after_approve_409_changes_nothing(world, monkeypatch):
    w = world
    ready(w)
    _approve_during_write(w, monkeypatch, "accept_remote")
    r = w["c"].post("/api/tasks/login_fix/files", json={"sha": NEW})
    assert r.status_code == 409 and r.json() == {"error": "run is queued, not waiting"}
    assert filestore.get(w["st"], w["task"].id)["edits"] is None


def test_bundle_upload_after_approve_409_changes_nothing(world, monkeypatch):
    w = world
    ready(w)
    _approve_during_write(w, monkeypatch, "accept_bundle")
    r = w["c"].post(f"/api/tasks/login_fix/files?base={GATE}", content=b"PACK",
                    headers={"content-type": "application/octet-stream"})
    assert r.status_code == 409
    assert filestore.get(w["st"], w["task"].id)["edits"] is None


def test_upload_runs_off_the_event_loop(world, monkeypatch):
    import asyncio
    w = world
    ready(w)
    seen = {}
    real = files.accept_remote

    def spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(*a, **kw)
    monkeypatch.setattr(files, "accept_remote", spy)
    r = w["c"].post("/api/tasks/login_fix/files", json={"sha": NEW})
    assert r.status_code == 200
    assert seen["on_loop"] is False
