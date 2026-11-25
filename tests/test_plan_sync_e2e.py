"""Plan sync end to end: worker <-> REST/MCP <-> repo cache, against a local bare origin."""
import os
import stat
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_worker import GIT_ENV, branches, env  # noqa: F401  (env is a fixture)
from test_worker_files import BRANCH, PLAN, penv  # noqa: F401  (penv is a fixture)

from slcloud import filestore, members, mcp_server, worker
from slcloud.api import create_app
from slcloud.auth import Principal

REF = "login_fix"


def sh(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **GIT_ENV})
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


@pytest.fixture
def rig(penv):
    e = penv
    claude = e.tmp / "bin" / "claude"
    old = 'open("' + PLAN + '", "w").write("# plan\\n"); '
    src = claude.read_text()
    assert old in src
    claude.write_text(src.replace(
        old, 'os.path.exists("' + PLAN + '") or open("' + PLAN + '", "w").write("# plan\\n"); '))
    pat = members.create_member(e.store, "w1", "pat", "pat@example.com", "member")
    who = {"cur": Principal("w1", "member", pat, "pat")}
    e.eng.submit("w1", e.origin, "plan the login fix", "claude", "main", owner=pat, name=REF)
    task_id = e.store.query("SELECT id FROM tasks")[0]["id"]
    app = create_app(e.deps.settings, e.store, e.eng, lambda: who["cur"])
    mcp = mcp_server.tools(e.eng, e.store, e.deps.settings, lambda: who["cur"])
    return SimpleNamespace(e=e, c=TestClient(app), who=who, mcp=mcp, id=task_id, pat=pat,
                           origin=e.origin, tmp=e.tmp)


def gate(r):
    assert worker.loop_once(r.e.deps) is True
    assert r.e.eng.get("w1", r.id).status == "waiting"


def publish(r):
    resp = r.c.post(f"/api/tasks/{REF}/publish")
    assert resp.status_code == 202 and resp.json() == {"state": "pending"}
    assert worker.loop_once(r.e.deps) is True
    resp = r.c.get(f"/api/tasks/{REF}/files")
    assert resp.status_code == 200
    return resp.json()


def clone(r, name="local"):
    d = str(r.tmp / name)
    sh("clone", "-q", "-b", BRANCH, r.origin, d)
    return d


def commit_edit(d, rel, text):
    path = os.path.join(d, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(text)
    sh("add", "-A", cwd=d)
    sh("commit", "-q", "-m", "edit " + rel, cwd=d)
    return sh("rev-parse", "HEAD", cwd=d)


def origin_show(r, spec):
    return subprocess.run(["git", "-C", r.origin, "show", spec], capture_output=True, text=True).stdout


def resume(r, answer="approve"):
    resp = r.c.post(f"/api/tasks/{REF}/respond", json={"answer": answer})
    assert resp.status_code == 200, resp.text
    assert worker.loop_once(r.e.deps) is True
    t = r.e.eng.get("w1", r.id)
    assert t.status == "done", t.error
    return t


def test_check1_pull_whole_branch(rig):
    gate(rig)
    body = publish(rig)
    assert body["state"] == "ready" and body["branch"] == BRANCH and body["pushed"] is True
    assert body["via"] == "remote" and body["plans"] == [PLAN] and body["edits"] is None
    assert {"a.txt", "out.txt", PLAN} <= set(body["files"])
    d = clone(rig)
    assert os.path.isfile(os.path.join(d, "a.txt")) and os.path.isfile(os.path.join(d, PLAN))
    assert sh("rev-parse", "HEAD", cwd=d) == body["sha"]
    plan_ev = [e for e in rig.e.eng.events(rig.id) if e["kind"] == "plan"][-1]
    assert plan_ev["data"] == {"plans": [PLAN]}


def test_check3_edits_recorded(rig):
    gate(rig)
    publish(rig)
    d = clone(rig)
    commit_edit(d, PLAN, "- [ ] T2 more\n")
    sha = commit_edit(d, "a.txt", "edited\n")
    sh("push", "-q", "origin", BRANCH, cwd=d)
    resp = rig.c.post(f"/api/tasks/{REF}/files", json={"sha": sha, "via": "remote"})
    assert resp.status_code == 200
    assert resp.json()["edits"] == {"sha": sha, "files": ["a.txt", PLAN]}
    got = rig.c.get(f"/api/tasks/{REF}/files").json()
    assert got["edits"]["sha"] == sha and set(got["edits"]["files"]) == {"a.txt", PLAN}


def test_check4_resume_names_files(rig):
    gate(rig)
    publish(rig)
    d = clone(rig)
    commit_edit(d, PLAN, "- [ ] T2 more\n")
    sha = commit_edit(d, "a.txt", "edited\n")
    sh("push", "-q", "origin", BRANCH, cwd=d)
    assert rig.c.post(f"/api/tasks/{REF}/files", json={"sha": sha}).status_code == 200
    t = resume(rig)
    out = origin_show(rig, f"{t.result['branch']}:out.txt")
    assert "a.txt" in out and PLAN in out and "Reread them" in out
    assert origin_show(rig, f"{t.result['branch']}:a.txt").endswith("edited\n")
    assert "T2 more" in origin_show(rig, f"{t.result['branch']}:{PLAN}")
    assert filestore.get(rig.e.store, rig.id) is None


def test_check5_branch_only_after_publish(rig):
    gate(rig)
    assert BRANCH not in branches(rig.origin)
    assert rig.c.get(f"/api/tasks/{REF}/files").status_code == 409  # never published
    publish(rig)
    assert BRANCH in branches(rig.origin)


def test_check6_mcp_write(rig):
    gate(rig)
    publish(rig)
    got = rig.mcp["read_file"](ref=REF, path=PLAN)
    assert got == {"path": PLAN, "text": "# plan\n"}
    out = rig.mcp["write_file"](ref=REF, path=PLAN, text=got["text"] + "- [ ] T9 mcp\n")
    assert out["files"] == [PLAN] and len(out["sha"]) == 40
    assert rig.c.get(f"/api/tasks/{REF}/files").json()["edits"] == {"sha": out["sha"], "files": [PLAN]}
    assert rig.mcp["read_file"](ref=REF, path=PLAN)["text"].endswith("T9 mcp\n")
    t = resume(rig)
    assert PLAN in origin_show(rig, f"{t.result['branch']}:out.txt")
    assert "T9 mcp" in origin_show(rig, f"{t.result['branch']}:{PLAN}")


def test_check7_bundle_fallback(rig):
    hook = os.path.join(rig.origin, "hooks", "pre-receive")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IEXEC)
    gate(rig)
    body = publish(rig)
    assert body["pushed"] is False and body["via"] == "slcloud" and BRANCH not in branches(rig.origin)
    dl = rig.c.get(f"/api/tasks/{REF}/files/bundle")
    assert dl.status_code == 200 and dl.headers["content-type"] == "application/octet-stream"
    bpath = str(rig.tmp / "gate.bundle")
    with open(bpath, "wb") as f:
        f.write(dl.content)
    d = str(rig.tmp / "viabundle")
    sh("init", "-q", "-b", "main", d)
    sh("fetch", "-q", bpath, f"+refs/runs/{rig.id}/gate:refs/heads/gate", cwd=d)
    sh("checkout", "-q", "gate", cwd=d)
    assert sh("rev-parse", "HEAD", cwd=d) == body["sha"] and os.path.isfile(os.path.join(d, PLAN))
    sha = commit_edit(d, "a.txt", "via bundle\n")
    up = str(rig.tmp / "up.bundle")
    sh("bundle", "create", up, "gate", cwd=d)
    with open(up, "rb") as f:
        resp = rig.c.post(f"/api/tasks/{REF}/files?base={body['sha']}", content=f.read(),
                          headers={"content-type": "application/octet-stream"})
    assert resp.status_code == 200
    assert resp.json()["edits"] == {"sha": sha, "files": ["a.txt"]}
    stale = rig.c.post(f"/api/tasks/{REF}/files?base=deadbeef", content=b"x",
                       headers={"content-type": "application/octet-stream"})
    assert stale.status_code == 409


def test_check8_push_after_cancel_409(rig):
    gate(rig)
    publish(rig)
    d = clone(rig)
    sha = commit_edit(d, "a.txt", "late\n")
    sh("push", "-q", "origin", BRANCH, cwd=d)
    rig.e.eng.cancel("w1", rig.id)
    resp = rig.c.post(f"/api/tasks/{REF}/files", json={"sha": sha})
    assert resp.status_code == 409 and resp.json() == {"error": "run is cancelled, not waiting"}
    assert filestore.get(rig.e.store, rig.id)["edits"] is None
    assert rig.mcp["write_file"](ref=REF, path="a.txt", text="x") == {
        "error": "run is cancelled, not waiting"}
    assert rig.c.post(f"/api/tasks/{REF}/publish").status_code == 409


def test_check8_push_after_approve_409(rig):
    gate(rig)
    publish(rig)
    d = clone(rig)
    sha = commit_edit(d, "a.txt", "late\n")
    sh("push", "-q", "origin", BRANCH, cwd=d)
    assert rig.c.post(f"/api/tasks/{REF}/respond", json={"answer": "approve"}).status_code == 200
    resp = rig.c.post(f"/api/tasks/{REF}/files", json={"sha": sha})
    assert resp.status_code == 409 and "not waiting" in resp.json()["error"]
    assert filestore.get(rig.e.store, rig.id)["edits"] is None


def test_check9_other_member_404(rig):
    gate(rig)
    publish(rig)
    chris = Principal("w1", "member", rig.e.chris, "chris")
    rig.who["cur"] = chris
    err = {"error": "not found"}
    for r in (rig.c.post(f"/api/tasks/pat/{REF}/publish"), rig.c.get(f"/api/tasks/pat/{REF}/files"),
              rig.c.get(f"/api/tasks/{rig.id}/files/bundle"),
              rig.c.post(f"/api/tasks/{rig.id}/files", json={"sha": "a" * 40})):
        assert r.status_code == 404 and r.json() == err
    assert rig.mcp["list_files"](ref=f"pat/{REF}") == err
    assert rig.mcp["write_file"](ref=rig.id, path="a.txt", text="x") == err
    assert filestore.get(rig.e.store, rig.id)["edits"] is None


def test_check10_rewrite_409(rig):
    gate(rig)
    publish(rig)
    d = clone(rig)
    sh("commit", "-q", "--amend", "-m", "rewritten", "--allow-empty", cwd=d)
    sha = sh("rev-parse", "HEAD", cwd=d)
    sh("push", "-q", "-f", "origin", BRANCH, cwd=d)
    resp = rig.c.post(f"/api/tasks/{REF}/files", json={"sha": sha})
    assert resp.status_code == 409 and resp.json() == {"error": "not a fast-forward of the gate commit"}
    assert filestore.get(rig.e.store, rig.id)["edits"] is None
    same = rig.c.post(f"/api/tasks/{REF}/files", json={"sha": filestore.get(rig.e.store, rig.id)["sha"]})
    assert same.status_code == 409
