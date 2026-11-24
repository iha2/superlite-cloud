import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from slcloud import members, worker
from slcloud.engine import Engine
from slcloud.sandbox import ProcessRunner
from slcloud.store import Store
from slcloud.vault import Vault

KEY = "sk-ant-xyz1234"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}

FAKE_CLAUDE = textwrap.dedent('''\
    #!{py}
    import json, os, sys, time
    with open("{log}", "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")

    def say(o):
        print(json.dumps(o), flush=True)

    def user():
        line = sys.stdin.readline()
        return json.loads(line)["message"]["content"] if line else None

    prompt = user()
    sess = "s_abc"
    say({{"type": "assistant", "session_id": sess, "message": {{"content": [
        {{"type": "tool_use", "name": "Edit", "input": {{"file_path": "out.txt"}}}}]}}}})
    open("out.txt", "a").write(prompt + "\\n")
    text = "all done"
    if prompt.startswith("slow"):
        time.sleep(0.8)
    if prompt.startswith("hang"):
        time.sleep(60)
    if prompt.startswith("plan"):
        text = "Plan ready. Reply approve to start"
    if prompt.startswith("wait"):
        say({{"type": "assistant", "session_id": sess, "message": {{"content": [
            {{"type": "text", "text": "waiting for you"}}]}}}})
        text = "got " + str(user())
    say({{"type": "result", "session_id": sess, "result": text, "is_error": False}})
''')

FAKE_CLI = textwrap.dedent('''\
    import subprocess, sys
    a = sys.argv[1:]
    def g(*x): subprocess.run(["git", *x], check=True)
    assert a[0] == "session"
    if a[1] == "create":
        g("checkout", "-q", "-b", "superlite/sessions/" + a[a.index("--slug") + 1])
    elif a[1] == "rename":
        g("branch", "-m", "superlite/sessions/" + a[a.index("--slug") + 1],
          "superlite/sessions/" + a[a.index("--to") + 1])
''')


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git("init", "-q", "--bare", "-b", "main", str(origin))
    git("init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("a\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "init", cwd=seed)
    git("push", "-q", str(origin), "main", cwd=seed)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.write_text(FAKE_CLAUDE.format(py=sys.executable, log=tmp_path / "claude.log"))
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    cli = tmp_path / "fakecli.py"
    cli.write_text(FAKE_CLI)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    store = Store(str(tmp_path / "t.db"))
    eng = Engine(store)
    vault = Vault(store, "mk")
    vault.set("w1", "anthropic", KEY)
    settings = SimpleNamespace(work_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
                               worker_image="img:1")
    deps = worker.Deps(eng, vault, ProcessRunner(), settings, poll=0.1,
                       cli=[sys.executable, str(cli)])
    chris = members.create_member(store, "w1", "chris", "chris@example.com", "member")
    return SimpleNamespace(eng=eng, deps=deps, origin=str(origin), tmp=tmp_path, chris=chris,
                           store=store)


def submit(env, prompt, name=None):
    env.eng.submit("w1", env.origin, prompt, "claude", "main", owner=env.chris, name=name)
    return env.eng.claim()


def branches(origin):
    out = subprocess.run(["git", "-C", origin, "for-each-ref", "--format=%(refname:short)",
                          "refs/heads"], capture_output=True, text=True).stdout
    return set(out.split())


def texts(env, task_id):
    return [(e["kind"], (e["data"] or {}).get("text")) for e in env.eng.events(task_id)]


def test_done_hands_back(env):
    t = submit(env, "Fix the login redirect")
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    assert r.result["pushed"] is True and r.result["branch"] == "superlite/sessions/login-fix"
    assert "superlite/sessions/login-fix" in branches(env.origin)
    assert ("handback", "handed back on superlite/sessions/login-fix") in texts(env, t.id)
    assert any(e["kind"] == "tool" for e in env.eng.events(t.id))
    assert not os.path.exists(os.path.join(env.deps.settings.work_dir, t.id))


def test_agent_state_removed_with_run_dir(env):
    from slcloud.sandbox import state_root
    t = submit(env, "Fix the login redirect")
    run_dir = os.path.join(env.deps.settings.work_dir, t.id)
    os.makedirs(os.path.join(state_root(run_dir), "claude"))
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    assert not os.path.exists(state_root(run_dir))


def test_waiting_keeps_checkout(env):
    t = submit(env, "plan the thing")
    r = worker.run_task(t, env.deps)
    assert r.status == "waiting" and r.session == "s_abc"
    run_dir = os.path.join(env.deps.settings.work_dir, t.id)
    assert os.path.isfile(os.path.join(run_dir, "out.txt"))
    env.eng.respond("w1", t.id, "approve")
    t2 = env.eng.claim()
    r2 = worker.run_task(t2, env.deps)
    assert r2.status == "done", r2.error
    calls = [json.loads(x) for x in open(env.tmp / "claude.log")]
    assert "--resume" not in calls[0] and calls[1][calls[1].index("--resume") + 1] == "s_abc"
    assert not os.path.exists(run_dir)


def test_two_runs_one_repo(env):
    a = submit(env, "Fix the login redirect")
    b = submit(env, "Dark mode for settings page")
    out = []
    ths = [threading.Thread(target=lambda t=t: out.append(worker.run_task(t, env.deps)))
           for t in (a, b)]
    [t.start() for t in ths]
    [t.join(60) for t in ths]
    assert sorted(r.status for r in out) == ["done", "done"], [r.error for r in out]
    assert {"superlite/sessions/login-fix", "superlite/sessions/dark-mode"} <= branches(env.origin)


def test_nudge_delivered(env):
    t = submit(env, "wait for it", name="nudge_me")
    box = []
    th = threading.Thread(target=lambda: box.append(worker.run_task(t, env.deps)))
    th.start()
    deadline = time.time() + 20
    while time.time() < deadline and not any(e["kind"] == "tool" for e in env.eng.events(t.id)):
        time.sleep(0.02)
    nid = env.eng.add_nudge(t.id, env.chris, "use v2")
    t0 = time.time()
    while time.time() - t0 < 0.5 and env.eng.pending_nudges(t.id):
        time.sleep(0.01)
    assert not env.eng.pending_nudges(t.id) and time.time() - t0 < 0.5
    th.join(30)
    nudges = [e["data"] for e in env.eng.events(t.id) if e["kind"] == "nudge"]
    assert nudges == [{"by": "chris", "text": "use v2"}]
    row = env.store.query("SELECT status FROM nudges WHERE id=?", (nid,))[0]
    assert row["status"] == "delivered"
    assert box[0].status == "done", box[0].error
    assert any(e["kind"] == "text" and e["data"]["text"] for e in env.eng.events(t.id))


def test_driver_fail_watch_only(env, monkeypatch):
    real = worker.get_driver

    def broken(agent, start, session=None, slug=None, workdir="/work"):
        d = real(agent, start, session, slug=slug, workdir=workdir)

        def boom(text):
            raise RuntimeError("no steer")

        d.nudge = boom
        return d

    monkeypatch.setattr(worker, "get_driver", broken)
    t = submit(env, "slow one", name="slow_one")
    nid = env.eng.add_nudge(t.id, env.chris, "x")
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    assert ("notice", "nudges are off") in texts(env, t.id)
    row = env.store.query("SELECT status FROM nudges WHERE id=?", (nid,))[0]
    assert row["status"] == "unsupported"


def test_no_git_in_run_dir(env, monkeypatch):
    real = subprocess.run
    seen = []

    def spy(argv, *a, **kw):
        if argv and argv[0] == "git":
            seen.append(kw.get("cwd"))
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    t = submit(env, "Fix the login redirect")
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    run_dir = os.path.join(env.deps.settings.work_dir, t.id)
    assert seen and run_dir not in seen


def test_no_key_fails(env):
    store = env.store
    store.exec("DELETE FROM keys")
    t = submit(env, "Fix the login redirect")
    r = worker.run_task(t, env.deps)
    assert r.status == "failed" and r.error == "no anthropic key"


def test_turn_end_shapes():
    assert worker._turn_end({"kind": "status", "status": "done"}) == "done"
    assert worker._turn_end({"kind": "status", "status": "error"}) == "error"
    assert worker._turn_end({"kind": "done"}) == "done"
    assert worker._turn_end({"kind": "error", "text": "x"}) == "error"
    assert worker._turn_end({"kind": "text"}) is None


def test_loop_once(env):
    assert worker.loop_once(env.deps) is False
    env.eng.submit("w1", env.origin, "Fix the login redirect", "claude", "main", owner=env.chris)
    assert worker.loop_once(env.deps) is True


def test_bad_base_fails_before_git(env):
    env.eng.submit("w1", env.origin, "Fix the login redirect", "claude", "--upload-pack=x", owner=env.chris)
    t = env.eng.claim()
    r = worker.run_task(t, env.deps)
    assert r.status == "failed" and r.error == "invalid base"


def test_commit_skips_secret_files(tmp_path):
    repo = tmp_path / "r"
    git("init", "-q", "-b", "main", str(repo))
    (repo / "sub").mkdir()
    for n in (".env", ".env.local", "sub/.env", "sub/id_rsa", "key.pem", ".npmrc", "ok.txt"):
        (repo / n).write_text("x\n")
    envv = {**os.environ, **worker._GIT_ENV, "SL_MSG": "m"}
    subprocess.run(["sh", "-c", worker._COMMIT], cwd=repo, env=envv, check=True)
    files = set(subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True).stdout.split())
    assert files == {"ok.txt"}


def _events(env, task_id, kind):
    return [e["data"] for e in env.eng.events(task_id) if e["kind"] == kind]


def test_result_event_on_done_and_waiting(env):
    t = submit(env, "Fix the login redirect")
    assert worker.run_task(t, env.deps).status == "done"
    assert _events(env, t.id, "result") == [{"text": "all done"}]
    kinds = [e["kind"] for e in env.eng.events(t.id)]
    assert kinds.index("result") < len(kinds) - 1 and env.eng.events(t.id)[-1]["data"] == {"to": "done"}
    p = submit(env, "plan the thing")
    assert worker.run_task(p, env.deps).status == "waiting"
    assert _events(env, p.id, "result") == [{"text": "Plan ready. Reply approve to start"}]


def test_rename_event_when_branch_taken(env):
    git("push", "-q", env.origin, "main:refs/heads/superlite/sessions/login-fix", cwd=env.tmp / "seed")
    t = submit(env, "Fix the login redirect")
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    new = r.name
    assert _events(env, t.id, "rename") == [
        {"from": "login_fix", "to": new, "why": "branch taken on origin"}]
    assert new != "login_fix"
    assert ("notice", f"renamed login_fix to {new}: branch taken on origin") in texts(env, t.id)


def test_handback_rename_avoids_other_live_run_name(env):
    from slcloud import names
    git("push", "-q", env.origin, "main:refs/heads/superlite/sessions/login-fix", cwd=env.tmp / "seed")
    other = submit(env, "unrelated work", name="login_fix_2")  # claimed: a live run holding the suffix
    assert other.name == "login_fix_2"
    t = submit(env, "Fix the login redirect")
    assert t.name == "login_fix"
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    assert r.name not in {"login_fix", "login_fix_2"}
    assert r.result["branch"] == "superlite/sessions/" + names.slug(r.name)
    assert r.result["branch"] in branches(env.origin)
    assert env.eng._load(other.id).name == "login_fix_2"


def _wait_for(pred, secs=20):
    end = time.time() + secs
    while time.time() < end and not pred():
        time.sleep(0.02)
    return pred()


def test_cancel_stops_run_without_handback(env):
    t = submit(env, "hang forever", name="cancel_me")
    box = []
    th = threading.Thread(target=lambda: box.append(worker.run_task(t, env.deps)))
    th.start()
    assert _wait_for(lambda: any(e["kind"] == "tool" for e in env.eng.events(t.id)))
    env.eng.cancel("w1", t.id)
    t0 = time.time()
    th.join(10)
    assert not th.is_alive() and time.time() - t0 < 5  # agent killed, not left to run 60s
    assert box[0].status == "cancelled"
    assert "superlite/sessions/cancel-me" not in branches(env.origin)
    assert not [b for b in branches(env.origin) if b != "main"]
    evs = env.eng.events(t.id)
    last = max(i for i, e in enumerate(evs) if e["kind"] == "status" and e["data"] == {"to": "cancelled"})
    assert last == len(evs) - 1, [e["kind"] for e in evs[last + 1:]]
    assert not any(e["kind"] in ("handback", "result") for e in evs)
    assert env.eng.get("w1", t.id).status == "cancelled"
    assert worker.loop_once(env.deps) is False  # slot free for the next run


def test_nudge_event_masks_secret(env):
    t = submit(env, "wait for it", name="nudge_me")
    th = threading.Thread(target=lambda: worker.run_task(t, env.deps))
    th.start()
    assert _wait_for(lambda: any(e["kind"] == "tool" for e in env.eng.events(t.id)))
    env.eng.add_nudge(t.id, env.chris, f"use {KEY} now")
    th.join(30)
    got = [e["data"] for e in env.eng.events(t.id) if e["kind"] == "nudge"]
    assert got and got[0]["by"] == "chris" and KEY not in got[0]["text"]


class _TurnProc:
    def __init__(self, gen):
        self.gen = gen
        self.killed = False

    def write(self, text):
        pass

    def lines(self):
        return self.gen()

    def close_stdin(self):
        pass

    def wait(self):
        return None

    def kill(self):
        self.killed = True


def test_cursor_queued_nudge_resumes_through_worker(env, monkeypatch):
    from slcloud.drivers.cursor import CursorDriver

    t = submit(env, "go cursor", name="cur_run")
    box = {"argvs": [], "seen": []}
    driver = []

    def status(nid):
        return env.store.query("SELECT status FROM nudges WHERE id=?", (nid,))[0]["status"]

    def turn1():
        yield '{"type":"system","session_id":"c_1"}'
        box["nid"] = env.eng.add_nudge(t.id, env.chris, "use v2")
        deadline = time.time() + 10
        while time.time() < deadline and not driver[0].queue:
            time.sleep(0.02)
        time.sleep(0.4)  # several pump polls: queued stays pending, is not re-queued
        box["seen"].append((status(box["nid"]), list(driver[0].queue)))
        yield '{"type":"result","result":"one","session_id":"c_1"}'

    def turn2():
        box["seen"].append((status(box["nid"]), list(driver[0].queue)))
        yield '{"type":"result","result":"two","session_id":"c_1"}'

    turns = iter([turn1, turn2])

    def start(argv):
        box["argvs"].append(argv)
        return _TurnProc(next(turns))

    def fake_get_driver(agent, _start, session=None, slug=None, workdir="/work"):
        driver.append(CursorDriver(start, session))
        return driver[0]

    monkeypatch.setattr(worker, "get_driver", fake_get_driver)
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    assert len(box["argvs"]) == 2 and box["argvs"][1][-1] == "use v2"
    assert box["argvs"][1][box["argvs"][1].index("--resume") + 1] == "c_1"
    assert box["seen"][0] == ("queued", ["use v2"])  # still pending until the resumed turn starts
    assert box["seen"][1] == ("delivered", [])
    assert status(box["nid"]) == "delivered"
    assert ("notice", "nudges are off") not in texts(env, t.id)
    assert ("nudge", "use v2") in texts(env, t.id)
    assert ("result", "two") in texts(env, t.id)


def test_failed_handshake_kills_sandbox(env, monkeypatch):
    procs = []

    class Boom:
        proc = None
        stopped = False

        def start(self, prompt):
            self.proc = _TurnProc(lambda: iter(()))
            procs.append(self.proc)
            raise RuntimeError("initialize: no response")

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(worker, "get_driver", lambda agent, start, session=None, slug=None, workdir="/work": Boom())
    t = submit(env, "go")
    r = worker.run_task(t, env.deps)
    assert r.status == "failed" and "no response" in r.error
    assert procs[0].killed


def test_process_run_note_names_actual_workdir(env):
    """Regression: the headless note hard-coded /work, but the process runner runs Claude in the run dir."""
    t = submit(env, "Fix the login redirect")
    r = worker.run_task(t, env.deps)
    assert r.status == "done", r.error
    run_dir = os.path.join(env.deps.settings.work_dir, t.id)
    argv = json.loads(open(env.tmp / "claude.log").readline())
    note = argv[argv.index("--append-system-prompt") + 1]
    assert f"already open in {run_dir} on its branch" in note
    assert "in /work " not in note and "only /work " not in note


def test_sandbox_workdir_per_runner(env):
    from slcloud.sandbox import DockerRunner
    cache = SimpleNamespace(path="/c")
    sb = worker._Sandbox(None, env.deps, "/data/work/t1", cache, {})
    assert sb.workdir == "/data/work/t1"  # process runner
    sb.deps = SimpleNamespace(runner=DockerRunner(), settings=env.deps.settings)
    assert sb.workdir == "/work"
