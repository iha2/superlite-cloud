import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from slcloud import app as slapp
from slcloud import auth, members
from slcloud.config import load_settings
from slcloud.sandbox import ProcessRunner
from slcloud.vault import Vault
from slcloud.worker import loop_once

GIT = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, env=GIT, check=True, capture_output=True, text=True).stdout


# Fake claude (stream-json over stdin/stdout): prompts starting "plan" end in a plan,
# "wait" holds the turn open for one nudge, anything else just edits a file and finishes.
FAKE_CLAUDE = textwrap.dedent("""\
    #!{py}
    import json, sys

    def say(o):
        print(json.dumps(o), flush=True)

    def user():
        line = sys.stdin.readline()
        return json.loads(line)["message"]["content"] if line else None

    prompt = user()
    say({{"type": "assistant", "session_id": "s1", "message": {{"content": [
        {{"type": "tool_use", "name": "Edit", "input": {{"file_path": "x.txt"}}}}]}}}})
    open("x.txt", "a").write(prompt + "\\n")
    text = "all done"
    if prompt.startswith("plan"):
        text = "Plan ready. Reply approve to start"
    if prompt.startswith("ask"):
        text = "Which login page: web or mobile?\\nReply to answer"
    if prompt == "web":
        text = "Plan ready. Reply approve to start"
    if prompt.startswith("wait"):
        say({{"type": "assistant", "session_id": "s1", "message": {{"content": [
            {{"type": "text", "text": "waiting for you"}}]}}}})
        text = "got " + str(user())
    say({{"type": "result", "session_id": "s1", "result": text, "is_error": False}})
""")

FAKE_CLI = textwrap.dedent("""\
    import subprocess, sys
    a = sys.argv[1:]
    def g(*x): subprocess.run(["git", *x], check=True)
    if a[1] == "create":
        g("checkout", "-q", "-b", "superlite/sessions/" + a[a.index("--slug") + 1])
    elif a[1] == "rename":
        g("branch", "-m", "superlite/sessions/" + a[a.index("--slug") + 1],
          "superlite/sessions/" + a[a.index("--to") + 1])
""")


def _setup(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    c = bin_ / "claude"
    c.write_text(FAKE_CLAUDE.format(py=sys.executable))
    c.chmod(c.stat().st_mode | stat.S_IEXEC)
    cli = tmp_path / "fakecli.py"
    cli.write_text(FAKE_CLI)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    seed = tmp_path / "seed"
    git("init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("a")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    remote = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(seed), str(remote))
    st = load_settings({"SLCLOUD_DATA": str(tmp_path / "d"), "SLCLOUD_MASTER_KEY": "k",
                        "SLCLOUD_SANDBOX": "process"})
    app = slapp.build(st, runner=ProcessRunner())
    app.state.worker_deps.cli = [sys.executable, str(cli)]
    app.state.worker_deps.poll = 0.1
    tok, _ = slapp.init_workspace(st, "team", member="chris")
    tok2, _ = slapp.init_workspace(st, "other", member="otto")
    store = app.state.store
    ws = store.query("SELECT workspace FROM members WHERE handle='chris'")[0]["workspace"]
    Vault(store, "k").set(ws, "anthropic", "sk-dummy-1234")
    pat = members.create_member(store, ws, "pat", "pat@example.com", "member")
    tok3 = auth.create_token(store, ws, "member", pat, "pat")
    hdr = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
    return app, st, str(remote), hdr(tok), hdr(tok2), hdr(tok3)


@pytest.fixture
def env(tmp_path, monkeypatch):
    app, st, remote, h, h2, h3 = _setup(tmp_path, monkeypatch)
    with TestClient(app) as client:
        yield client, app, remote, h, h2, h3


@pytest.fixture
def live(tmp_path, monkeypatch):
    """The same app on a real socket, so the SSE stream can be read while the run is live."""
    app, st, remote, h, *_ = _setup(tmp_path, monkeypatch)
    srv = slapp._ProbeServer(app, tempfile.mkdtemp(prefix="slcloud-e2e-"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://%s:%d" % srv.server_address
    for _ in range(100):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    try:
        yield base, app, remote, h
    finally:
        srv.server_close()


def submit(client, h, remote, prompt="add x"):
    r = client.post("/api/tasks", json={"repo": remote, "prompt": prompt, "base": "main"}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_flow_approve(env):
    client, app, remote, h, *_ = env
    tid = submit(client, h, remote, "plan add x")
    assert loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "waiting" and "Reply approve to start" in t["plan"]
    assert client.post(f"/api/tasks/{tid}/respond", json={"answer": "approve"}, headers=h).json()["status"] == "queued"
    assert loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "done", t
    assert t["result"]["pushed"] is True
    assert t["result"]["branch"] in git("branch", cwd=remote)
    assert not loop_once(app.state.worker_deps)


def test_flow_human_question_then_plan(env):
    client, app, remote, h, *_ = env
    row = app.state.store.query("SELECT id, workspace FROM members WHERE handle='chris'")[0]
    t = app.state.engine.submit(row["workspace"], remote, "ask fix the login bug", "claude", "main",
                                owner=row["id"], name="login_fix", orchestrator="human")
    tid = t.id
    assert loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert (t["status"], t["waiting_on"], t["orchestrator"]) == ("waiting", "question", "human")
    q = [e for e in client.get(f"/api/tasks/{tid}/events", headers=h).json() if e["kind"] == "question"]
    assert q[0]["data"] == {"run": "login_fix", "text": "Which login page: web or mobile?"}
    r = client.post(f"/api/tasks/{tid}/respond", json={"answer": "web"}, headers=h).json()
    assert (r["status"], r["waiting_on"]) == ("queued", None)
    assert loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert (t["status"], t["waiting_on"]) == ("waiting", "plan") and "Reply approve to start" in t["plan"]
    client.post(f"/api/tasks/{tid}/respond", json={"answer": "approve"}, headers=h)
    assert loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "done" and t["result"]["pushed"] is True


def test_changes_revised_plan(env):
    client, app, remote, h, *_ = env
    tid = submit(client, h, remote, "plan add x")
    loop_once(app.state.worker_deps)
    client.post(f"/api/tasks/{tid}/respond", json={"answer": "plan again: smaller"}, headers=h)
    loop_once(app.state.worker_deps)
    t = client.get(f"/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "waiting" and t["plan"].startswith("Plan ready")


def test_e2e_watch_and_nudge(live):
    base, app, remote, h = live
    r = httpx.post(base + "/api/tasks", headers=h, timeout=10,
                   json={"repo": remote, "prompt": "wait for it", "base": "main"})
    assert r.status_code == 200, r.text
    tid, name = r.json()["id"], r.json()["name"]
    worker = threading.Thread(target=loop_once, args=(app.state.worker_deps,), daemon=True)
    worker.start()
    seen, nudged = [], False
    deadline = time.time() + 40
    with httpx.stream("GET", f"{base}/api/runs/{name}/events", headers=h, timeout=45) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("event: "):
                seen.append(line[7:])
            if "tool" in seen and not nudged:
                nudged = True
                n = httpx.post(f"{base}/api/runs/{name}/nudge", headers=h, json={"text": "use v2"})
                assert n.status_code == 202
            if line.startswith("data: ") and "nudge from chris" in line:
                break
            assert time.time() < deadline, seen
    worker.join(40)
    t = httpx.get(f"{base}/api/tasks/{tid}", headers=h).json()
    assert t["status"] == "done", t
    assert t["result"]["branch"] in git("branch", cwd=remote)


def test_e2e_non_owner_404(env):
    client, app, remote, h, h2, h3 = env
    tid = submit(client, h, remote)
    for call in (lambda: client.get(f"/api/tasks/{tid}", headers=h3),
                 lambda: client.get(f"/api/runs/{tid}/events", headers=h3),
                 lambda: client.post(f"/api/runs/{tid}/nudge", headers=h3, json={"text": "hi"}),
                 lambda: client.get(f"/api/runs/{tid}/attach", headers=h3)):
        r = call()
        assert r.status_code == 404 and r.json() == {"error": "not found"}
    assert client.post(f"/api/runs/{tid}/nudge", headers=h, json={"text": "hi"}).status_code == 202


def test_mcp_over_http(env):
    client, app, remote, h, *_ = env
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
    client, app, remote, h, h2, _ = env
    assert client.get("/api/health").status_code == 200
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


@pytest.mark.skipif(os.environ.get("SLCLOUD_DOCKER_E2E") != "1", reason="needs docker")
def test_docker_start_e2e(monkeypatch):
    from pathlib import Path

    from slcloud.sandbox import DockerRunner, Job, Mount

    base = Path.home() / ".cache" / "slcloud-e2e"
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=base))
    ro = Path(tempfile.mkdtemp(dir=base))
    for d in (work, ro):
        d.chmod(0o777)
    (ro / "f.txt").write_text("ro-data")
    monkeypatch.setenv("SLCLOUD_E2E_HOST_ONLY", "leak")
    runner = DockerRunner()
    image = "docker.io/library/alpine:3.20"

    def names():
        ps = subprocess.run([runner.docker_bin, "ps", "-a", "--format", "{{.Names}}"],
                            capture_output=True, text=True, check=True)
        return [n for n in ps.stdout.split() if n.startswith("slcloud-")]

    before = set(names())
    try:
        # 1. long-lived echo loop: two lines in, two lines out, process alive between them
        p = runner.start(Job(image, ["sh", "-c", 'while read l; do echo "got:$l"; done'], {},
                             str(work), timeout_s=120))
        out = p.lines()
        p.write("one\n")
        assert next(out) == "got:one"
        assert p._p.poll() is None
        p.write("two\n")
        assert next(out) == "got:two"
        assert p._p.poll() is None
        assert set(names()) - before  # the named container is running
        p.close_stdin()
        assert p.wait().code == 0
        assert not set(names()) - before  # 3. container removed after close

        # 2. read-only mount readable, write fails; 4. only job.env is visible inside
        script = ('cat /ro/f.txt; echo; (echo x > /ro/new.txt) 2>/dev/null && echo WROTE || echo RO; '
                  'echo "A=$E2E_A"; echo "H=$SLCLOUD_E2E_HOST_ONLY"')
        p = runner.start(Job(image, ["sh", "-c", script], {"E2E_A": "val"}, str(work),
                             timeout_s=120, mounts=[Mount(str(ro), "/ro")]))
        p.close_stdin()
        got = list(p.lines())
        assert p.wait().code == 0
        assert got == ["ro-data", "RO", "A=val", "H="], got
        assert not (ro / "new.txt").exists()

        # stopping a live Proc must not leave a container behind
        p = runner.start(Job(image, ["sleep", "60"], {}, str(work), timeout_s=120))
        time.sleep(3)
        assert set(names()) - before
        p.kill()
        p.wait()
        for _ in range(20):
            if not set(names()) - before:
                break
            time.sleep(0.5)
        assert not set(names()) - before
    finally:
        for n in set(names()) - before:
            subprocess.run([runner.docker_bin, "rm", "-f", n], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(ro, ignore_errors=True)


def test_mcp_auth_isolation_and_no_secrets(env):
    client, app, remote, h, h2, _ = env
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
    client, app, remote, h, h2, _ = env
    tid = submit(client, h, remote)
    assert client.get(f"/api/tasks/{tid}/bundle").status_code == 401
    assert client.get(f"/api/tasks/{tid}/bundle", headers=h2).status_code == 404
    assert client.get("/api/tasks/..%2F..%2Fetc%2Fpasswd/bundle", headers=h).status_code == 404


def test_init_workspace_token_valid_and_run_worker_stops(env):
    client, app, remote, h, *_ = env
    st = app.state.worker_deps.settings
    tok, _ = slapp.init_workspace(st, "third", member="thea")
    assert client.get("/api/tasks", headers={"Authorization": f"Bearer {tok}"}).json() == []
    submit(client, h, remote)
    n = {"i": 0}

    def stop():
        n["i"] += 1
        return n["i"] > 2

    slapp.run_worker(st, runner=ProcessRunner(), poll=0.01, stop=stop)
    assert n["i"] == 3
