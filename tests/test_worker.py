import os
from types import SimpleNamespace

import pytest

from slcloud import worker
from slcloud.gitops import HandBack
from slcloud.sandbox import JobResult

REAL = os.environ.get("SUPERLITE_REAL") == "1"
KEY = "sk-ant-xyz1234"
NOTICE = "Run `superlite-cli orchestrator set ai` first."


def fake_mask(text, secrets):
    for s in secrets:
        text = text.replace(s, "***" + s[-4:])
    return text


class FakeEngine:
    def __init__(self, task=None):
        self.queue = [task] if task else []
        self.calls = []
        self.events = []

    def claim(self):
        return self.queue.pop(0) if self.queue else None

    def transition(self, task_id, status, **fields):
        self.calls.append((task_id, status, fields))
        return SimpleNamespace(id=task_id, status=status, **fields)

    def add_event(self, task_id, kind, data):
        self.events.append((task_id, kind, data))


class FakeVault:
    def __init__(self, keys):
        self.keys = keys

    def get(self, ws, provider):
        return self.keys.get(provider)


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.jobs = []

    def run(self, job):
        self.jobs.append(job)
        return self.result


def mk_task(**kw):
    base = dict(id="t_0123456789ab", ws="w1", repo="/r.git", prompt="add x",
                agent="claude", base="HEAD", session=None, answer=None, status="running")
    base.update(kw)
    return SimpleNamespace(**base)


def ok_out(text="all done", session="s1"):
    import json
    return json.dumps({"type": "result", "result": text, "session_id": session, "is_error": False})


@pytest.fixture(autouse=True)
def _mask(monkeypatch):
    if not REAL:
        monkeypatch.setattr(worker, "mask", fake_mask)


def mk_deps(tmp_path, task, result, keys=None):
    eng = FakeEngine(task)
    runner = FakeRunner(result)
    clones, hbs = [], []

    def clone(repo, base, dest):
        clones.append((repo, base, dest))
        return "abc"

    def hand_back(workdir, branch, out_dir, push=True):
        hbs.append((workdir, branch, out_dir, push))
        return HandBack(branch=branch, sha="9c1e", pushed=True, bundle=None, fetch="git fetch origin x")

    settings = SimpleNamespace(work_dir=str(tmp_path / "work"), data_dir=str(tmp_path / "data"),
                               worker_image="img:1")
    vault = FakeVault({"anthropic": KEY, "gemini": "g-key-9999"} if keys is None else keys)
    deps = worker.Deps(eng, vault, runner, settings, clone=clone, hand_back=hand_back)
    return deps, eng, runner, clones, hbs


def test_loop_once_empty(tmp_path):
    deps, *_ = mk_deps(tmp_path, None, JobResult(0, "", "", False))
    assert worker.loop_once(deps) is False


def test_loop_once_runs(tmp_path):
    deps, eng, *_ = mk_deps(tmp_path, mk_task(), JobResult(0, ok_out(), "", False))
    assert worker.loop_once(deps) is True
    assert eng.calls[-1][1] == "done"


def test_no_key_fails(tmp_path):
    deps, eng, runner, clones, _ = mk_deps(tmp_path, mk_task(), JobResult(0, "", "", False), keys={})
    worker.run_task(mk_task(), deps)
    assert eng.calls == [("t_0123456789ab", "failed", {"error": "no anthropic key"})]
    assert runner.jobs == [] and clones == []


def test_plan_waits(tmp_path):
    out = ok_out("Plan ready. Reply approve to start")
    deps, eng, runner, clones, _ = mk_deps(tmp_path, None, JobResult(0, out, "", False))
    worker.run_task(mk_task(), deps)
    assert eng.calls == [("t_0123456789ab", "waiting",
                          {"session": "s1", "plan": "Plan ready. Reply approve to start"})]
    assert len(clones) == 1
    job = runner.jobs[0]
    assert job.env == {"ANTHROPIC_API_KEY": KEY}
    assert job.workdir == str(tmp_path / "work" / "t_0123456789ab")
    assert job.command[-1] == "add x"


def test_resume_uses_answer(tmp_path):
    deps, eng, runner, clones, _ = mk_deps(tmp_path, None, JobResult(0, ok_out(), "", False))
    worker.run_task(mk_task(session="s1", answer="approve"), deps)
    assert clones == []
    assert runner.jobs[0].command[-1] == "approve"
    assert runner.jobs[0].command[-3:-1] == ["--resume", "s1"]


def test_key_masked(tmp_path):
    deps, eng, *_ = mk_deps(tmp_path, None, JobResult(1, "", f"boom {KEY} bad", False))
    worker.run_task(mk_task(), deps)
    _, status, f = eng.calls[-1]
    assert status == "failed" and KEY not in f["error"] and "***1234" in f["error"]


def test_nonzero_exit_failed(tmp_path):
    deps, eng, *_ = mk_deps(tmp_path, None, JobResult(2, "", "x" * 3000, False))
    worker.run_task(mk_task(), deps)
    _, status, f = eng.calls[-1]
    assert status == "failed" and len(f["error"]) == 2000


def test_timed_out_failed(tmp_path):
    deps, eng, *_ = mk_deps(tmp_path, None, JobResult(-1, "", "", True))
    worker.run_task(mk_task(), deps)
    assert eng.calls[-1] == ("t_0123456789ab", "failed", {"error": "timed out"})


def test_no_resume_agent_ai_notice(tmp_path):
    deps, eng, runner, *_ = mk_deps(tmp_path, None, JobResult(0, "plain text", "", False))
    worker.run_task(mk_task(agent="gemini"), deps)
    assert any(c.startswith(NOTICE) for c in runner.jobs[0].command)
    assert runner.jobs[0].env == {"GEMINI_API_KEY": "g-key-9999"}
    assert any(e[1] == "notice" for e in eng.events)


def test_done_result(tmp_path):
    deps, eng, runner, clones, hbs = mk_deps(tmp_path, None, JobResult(0, ok_out(), "", False))
    worker.run_task(mk_task(), deps)
    assert hbs == [(str(tmp_path / "work" / "t_0123456789ab"), "slcloud/t_0123456789ab",
                    str(tmp_path / "data" / "bundles"), True)]
    assert eng.calls[-1] == ("t_0123456789ab", "done", {"result": {
        "branch": "slcloud/t_0123456789ab", "sha": "9c1e", "pushed": True,
        "bundle": None, "fetch": "git fetch origin x"}})


def test_bad_agent_and_vault_error_fail_masked(tmp_path):
    deps, eng, *_ = mk_deps(tmp_path, None, JobResult(0, "", "", False))
    worker.run_task(mk_task(agent="nope"), deps)
    assert eng.calls[-1][1] == "failed" and "unknown agent" in eng.calls[-1][2]["error"]

    def boom(ws, provider):
        raise RuntimeError("vault down")
    deps.vault.get = boom
    worker.run_task(mk_task(), deps)
    assert eng.calls[-1][1] == "failed" and "vault down" in eng.calls[-1][2]["error"]


def test_clone_failure_masked(tmp_path):
    deps, eng, runner, *_ = mk_deps(tmp_path, None, JobResult(0, "", "", False))

    def bad_clone(repo, base, dest):
        raise RuntimeError(f"fatal {KEY}")
    deps.clone = bad_clone
    worker.run_task(mk_task(), deps)
    _, status, f = eng.calls[-1]
    assert status == "failed" and KEY not in f["error"] and runner.jobs == []


def test_plan_masked_and_env_only_key(tmp_path):
    out = ok_out(f"plan {KEY} Reply approve to start")
    deps, eng, runner, *_ = mk_deps(tmp_path, None, JobResult(0, out, "", False))
    worker.run_task(mk_task(), deps)
    assert KEY not in eng.calls[-1][2]["plan"]
    assert "--bare" in runner.jobs[0].command
    assert list(runner.jobs[0].env) == ["ANTHROPIC_API_KEY"]


def test_loop_survives_transition_error(tmp_path):
    deps, eng, *_ = mk_deps(tmp_path, mk_task(), JobResult(0, ok_out(), "", False))

    def bad(*a, **k):
        raise RuntimeError("cancelled")
    eng.transition = bad
    assert worker.loop_once(deps) is True


def _mk_workdir(tmp_path, deps):
    wd = tmp_path / "work" / "t_0123456789ab"

    def clone(repo, base, dest):
        os.makedirs(dest)
        (wd / "f.txt").write_text("x")
        return "abc"
    deps.clone = clone
    return wd


def test_workdir_removed_when_done(tmp_path):
    deps, *_ = mk_deps(tmp_path, None, JobResult(0, ok_out(), "", False))
    wd = _mk_workdir(tmp_path, deps)
    worker.run_task(mk_task(), deps)
    assert not wd.exists()


def test_workdir_removed_when_failed(tmp_path):
    deps, *_ = mk_deps(tmp_path, None, JobResult(2, "", "boom", False))
    wd = _mk_workdir(tmp_path, deps)
    worker.run_task(mk_task(), deps)
    assert not wd.exists()


def test_workdir_kept_when_waiting(tmp_path):
    out = ok_out("Plan ready. Reply approve to start")
    deps, *_ = mk_deps(tmp_path, None, JobResult(0, out, "", False))
    wd = _mk_workdir(tmp_path, deps)
    worker.run_task(mk_task(), deps)
    assert wd.exists()
