import json
import os
import shutil
import stat
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient

from slcloud import app as slapp
from slcloud.config import load_settings
from slcloud.sandbox import ProcessRunner
from slcloud.worker import loop_once

PLAN = "Plan: add x. Reply approve to start"
GIT = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, env=GIT, check=True, capture_output=True, text=True).stdout


FAKE = """#!/bin/sh
# fake claude: plans first, after --resume it writes a file
case "$*" in
*--resume*)
  echo "$*" | grep -q revise && echo '{"type":"result","result":"Plan v2. Reply approve to start","session_id":"s2","is_error":false}' && exit 0
  echo done > x.txt
  echo '{"type":"result","result":"all done","session_id":"s1","is_error":false}';;
*) echo '{"type":"result","result":"Plan v1. Reply approve to start","session_id":"s1","is_error":false}';;
esac
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    c = bin_ / "claude"
    c.write_text(FAKE)
    c.chmod(c.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    seed = tmp_path / "seed"
    git("init", "-q", str(seed))
    (seed / "a.txt").write_text("a")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(seed), str(remote))
    st = load_settings({"SLCLOUD_DATA": str(tmp_path / "d"), "SLCLOUD_MASTER_KEY": "k",
                        "SLCLOUD_SANDBOX": "process"})
    app = slapp.build(st, runner=ProcessRunner())
    ws, tok = slapp.init_workspace(st, "team")
    ws2, tok2 = slapp.init_workspace(st, "other")
    from slcloud.vault import Vault

    Vault(app.state.store, "k").set(ws, "anthropic", "sk-dummy-1234")
    with TestClient(app) as client:
        yield client, app, str(remote), {"Authorization": f"Bearer {tok}"}, {"Authorization": f"Bearer {tok2}"}


def submit(client, h, remote):
    r = client.post("/api/tasks", json={"repo": remote, "prompt": "add x", "base": "HEAD"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_flow_approve(env):
    client, app, remote, h, _ = env
    tid = submit(client, h, remote)
    assert loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "waiting" and "Reply approve to start" in t["plan"]
    assert client.post(f"/api/tasks/{tid}/respond", json={"answer": "approve"}, headers=h).json()["status"] == "queued"
    assert loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "done", t
    assert t["result"]["pushed"] is True
    assert f"slcloud/{tid}" in git("branch", cwd=remote)
    assert not loop_once(app.state.worker_deps)


def test_changes_revised_plan(env):
    client, app, remote, h, _ = env
    tid = submit(client, h, remote)
    loop_once(app.state.worker_deps)
    client.post(f"/api/tasks/{tid}/respond", json={"answer": "revise: smaller"}, headers=h)
    loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "waiting" and t["plan"].startswith("Plan v2")


def test_mcp_over_http(env):
    client, app, remote, h, _ = env
    mh = {**h, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def rpc(method, params, i):
        r = client.post("/mcp", headers=mh, json={"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        assert r.status_code == 200, r.text
        return r.json()["result"]

    names = {t["name"] for t in rpc("tools/list", {}, 1)["tools"]}
    assert {"submit_task", "get_task", "fetch_command", "team_lessons"} <= names
    res = rpc("tools/call", {"name": "submit_task", "arguments": {"repo": remote, "prompt": "p"}}, 2)
    payload = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    payload = payload.get("result", payload)
    assert payload["status"] == "queued" and payload["task_id"].startswith("t_")
    assert client.get(f"/api/tasks/{payload['task_id']}", headers=h).status_code == 200


def test_401_and_cross_workspace_404(env):
    client, app, remote, h, h2 = env
    assert client.get("/api/health").status_code == 200
    assert client.get("/setup").status_code == 200
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/api/tasks", headers={"Authorization": "Bearer slc_bad"}).status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    tid = submit(client, h, remote)
    assert client.get(f"/api/tasks/{tid}", headers=h2).status_code == 404
    assert client.post(f"/api/tasks/{tid}/cancel", headers=h2).status_code == 404


@pytest.mark.skipif(os.environ.get("SLCLOUD_DOCKER_E2E") != "1", reason="needs docker")
def test_docker_e2e():
    from pathlib import Path

    from slcloud.sandbox import DockerRunner, Job

    # The workdir lives under $HOME, not tmp_path: a podman machine VM only shares $HOME with
    # the host, so a pytest tmp dir would not be mountable into the sandbox container.
    base = Path.home() / ".cache" / "slcloud-e2e"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=base))
    work.chmod(0o777)  # the container user may differ from the host user
    try:
        (work / "in.txt").write_text("hello")
        runner = DockerRunner()
        image = "docker.io/library/alpine:3.20"

        def run(cmd, timeout_s=120, env=None):
            return runner.run(Job(image, cmd, env or {}, str(work), timeout_s=timeout_s))

        r = run(["sh", "-c", 'cat in.txt; echo -n "$E2E_VAR" > out.txt; echo -n "$E2E_VAR"; '
                             "grep CapEff /proc/self/status"], env={"E2E_VAR": "v-123"})
        assert r.code == 0, r.stderr
        assert r.stdout.startswith("hellov-123")  # mounted file readable, env var passed
        assert (work / "out.txt").read_text() == "v-123"  # written back to the host
        assert r.stdout.split("CapEff:")[1].strip() == "0000000000000000"
        assert run(["sh", "-c", "exit 7"]).code == 7
        t = run(["sleep", "30"], timeout_s=3)
        assert t.timed_out is True
        ps = subprocess.run([runner.docker_bin, "ps", "-a", "--format", "{{.Names}}"],
                            capture_output=True, text=True, check=True)
        assert not [n for n in ps.stdout.split() if n.startswith("slcloud-")]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_mcp_auth_isolation_and_no_secrets(env):
    client, app, remote, h, h2 = env
    mh = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert client.post("/mcp", headers={**mh, "Authorization": "Bearer slc_bad"}, json=body).status_code == 401
    tid = submit(client, h, remote)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_task", "arguments": {"task_id": tid}}}
    r = client.post("/mcp", headers={**mh, **h2}, json=call)
    assert "not found" in r.text and tid not in r.text.replace("task_id", "")
    loop_once(app.state.worker_deps)
    out = client.get(f"/api/tasks/{tid}", headers=h).text + client.get(f"/api/tasks/{tid}/events", headers=h).text
    out += client.get("/api/keys", headers=h).text
    assert "sk-dummy-1234" not in out


def test_bundle_requires_auth_and_workspace(env):
    client, app, remote, h, h2 = env
    tid = submit(client, h, remote)
    assert client.get(f"/api/tasks/{tid}/bundle").status_code == 401
    assert client.get(f"/api/tasks/{tid}/bundle", headers=h2).status_code == 404
    assert client.get("/api/tasks/..%2F..%2Fetc%2Fpasswd/bundle", headers=h).status_code == 404


def test_init_workspace_token_valid_and_run_worker_stops(env):
    client, app, remote, h, _ = env
    st = app.state.worker_deps.settings
    ws, tok = slapp.init_workspace(st, "third")
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {tok}"}).json() == []
    submit(client, h, remote)
    n = {"i": 0}

    def stop():
        n["i"] += 1
        return n["i"] > 2

    slapp.run_worker(st, runner=ProcessRunner(), poll=0.01, stop=stop)
    assert n["i"] == 3
