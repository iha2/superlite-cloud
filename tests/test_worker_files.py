import os
import subprocess

import pytest
from test_worker import GIT_ENV, branches, env, git, submit  # noqa: F401  (env is a fixture)

from slcloud import files, filestore, repocache, worker

PLAN = "docs/plans/2026-11-24-login-fix.review.md"
BRANCH = "superlite/sessions/login-fix"


@pytest.fixture
def penv(env):
    claude = env.tmp / "bin" / "claude"
    src = claude.read_text().replace(
        'open("out.txt", "a")',
        'os.makedirs("docs/plans", exist_ok=True); open("' + PLAN + '", "w").write("# plan\\n"); '
        'open("out.txt", "a")')
    claude.write_text(src)
    return env


def gated(env, name="login_fix"):
    t = submit(env, "plan the login fix", name=name)
    r = worker.run_task(t, env.deps)
    assert r.status == "waiting", r.error
    return t


def cache_of(env, t):
    return repocache.cache_for(env.deps.settings.data_dir, t.ws, t.repo)


def origin_show(env, spec):
    return subprocess.run(["git", "-C", env.origin, "show", spec], capture_output=True,
                          text=True).stdout


def test_gate_commits_and_records_plans(penv):
    t = gated(penv)
    row = filestore.get(penv.store, t.id)
    assert row["state"] == "gated" and row["plans"] == [PLAN]
    cache = cache_of(penv, t)
    assert subprocess.run(["git", "-C", cache.path, "rev-parse", files.gate_ref(t.id)],
                          capture_output=True, text=True).stdout.strip() == row["sha"]
    assert PLAN in files.list_files(cache, row["sha"])
    evs = penv.eng.events(t.id)
    plan_i = max(i for i, e in enumerate(evs) if e["kind"] == "plan")
    assert evs[plan_i]["data"] == {"plans": [PLAN]}
    assert plan_i == len(evs) - 2 and evs[-1]["data"]["to"] == "waiting"


def test_gate_no_host_git_in_run_dir(penv, monkeypatch):
    real = subprocess.run
    seen = []

    def spy(argv, *a, **kw):
        if argv and argv[0] == "git":
            seen.append(kw.get("cwd"))
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    t = gated(penv)
    assert seen and os.path.join(penv.deps.settings.work_dir, t.id) not in seen


def test_publish_pushes_branch(penv):
    t = gated(penv)
    sha = filestore.get(penv.store, t.id)["sha"]
    filestore.request_publish(penv.store, t.id)
    assert worker.publish_pending(penv.deps) is True
    row = filestore.get(penv.store, t.id)
    assert row["state"] == "ready" and row["pushed"] is True and row["branch"] == BRANCH
    assert BRANCH in branches(penv.origin)
    assert origin_show(penv, f"{BRANCH}:out.txt")
    ev = [e for e in penv.eng.events(t.id) if e["kind"] == "published"][-1]
    assert ev["data"] == {"branch": BRANCH, "sha": sha, "pushed": True}
    assert worker.publish_pending(penv.deps) is False


def test_publish_bundle_fallback(penv):
    t = gated(penv)
    git("branch", BRANCH, "main", cwd=penv.origin)  # origin already holds the branch
    filestore.request_publish(penv.store, t.id)
    assert worker.publish_pending(penv.deps) is True
    row = filestore.get(penv.store, t.id)
    want = os.path.join(penv.deps.settings.data_dir, "bundles", f"{t.id}.gate.bundle")
    assert row["pushed"] is False and row["bundle"] == want and os.path.isfile(want)


def test_publish_before_claim(penv):
    t = gated(penv)
    penv.eng.submit("w1", penv.origin, "other", "claude", "main", owner=penv.chris)
    filestore.request_publish(penv.store, t.id)
    assert worker.loop_once(penv.deps) is True
    assert filestore.get(penv.store, t.id)["state"] == "ready"
    rows = penv.store.query("SELECT status FROM tasks WHERE id!=?", (t.id,))
    assert [r["status"] for r in rows] == ["queued"]


def _edit(penv, t):
    cache = cache_of(penv, t)
    gate = filestore.get(penv.store, t.id)["sha"]
    sha, edited = files.write_file(cache, t.id, gate, "README.md", "edited text\n")
    filestore.set_edits(penv.store, t.id, sha, edited)
    return edited


def test_resume_fast_forwards_edits(penv):
    t = gated(penv)
    edited = _edit(penv, t)
    assert edited == ["README.md"]
    penv.eng.respond("w1", t.id, "approve")
    r = worker.run_task(penv.eng.claim(), penv.deps)
    assert r.status == "done", r.error
    assert origin_show(penv, f"{r.result['branch']}:README.md") == "edited text\n"
    out = origin_show(penv, f"{r.result['branch']}:out.txt")
    assert worker.answer_for("approve", edited) in out
    assert filestore.get(penv.store, t.id) is None
    cache = cache_of(penv, t)
    p = subprocess.run(["git", "-C", cache.path, "rev-parse", "-q", "--verify",
                        files.edits_ref(t.id)], capture_output=True)
    assert p.returncode != 0


def test_resume_without_edits_unchanged(penv):
    t = gated(penv)
    penv.eng.respond("w1", t.id, "approve")
    r = worker.run_task(penv.eng.claim(), penv.deps)
    assert r.status == "done", r.error
    out = origin_show(penv, f"{r.result['branch']}:out.txt")
    assert out.splitlines()[-1] == "approve"
    assert filestore.get(penv.store, t.id) is None


def test_handback_after_publish_keeps_name(penv):
    t = gated(penv)
    filestore.request_publish(penv.store, t.id)
    assert worker.publish_pending(penv.deps)
    penv.eng.respond("w1", t.id, "approve")
    r = worker.run_task(penv.eng.claim(), penv.deps)
    assert r.status == "done", r.error
    assert r.result["branch"] == BRANCH and r.result["pushed"] is True
    assert "superlite/sessions/login-fix-2" not in branches(penv.origin)


def test_answer_for():
    assert worker.answer_for("approve", []) == "approve"
    assert worker.answer_for("approve", ["README.md", "docs/plans/x.review.md"]) == (
        "approve\n\nBefore approving, the user edited these files on the branch: "
        "README.md, docs/plans/x.review.md. Reread them before you build.")


def test_second_gate_after_publish_keeps_name(penv):
    t = gated(penv)
    filestore.request_publish(penv.store, t.id)
    assert worker.publish_pending(penv.deps)
    penv.eng.respond("w1", t.id, "plan more")
    r = worker.run_task(penv.eng.claim(), penv.deps)
    assert r.status == "waiting", r.error
    filestore.request_publish(penv.store, t.id)
    assert worker.publish_pending(penv.deps)
    assert filestore.get(penv.store, t.id)["pushed"] is True
    penv.eng.respond("w1", t.id, "approve")
    r = worker.run_task(penv.eng.claim(), penv.deps)
    assert r.status == "done", r.error
    assert r.result["branch"] == BRANCH and r.result["pushed"] is True
    assert "superlite/sessions/login-fix-2" not in branches(penv.origin)
