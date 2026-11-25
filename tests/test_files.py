import os
import subprocess

import pytest

from slcloud import files
from slcloud.errors import NotFound
from slcloud.repocache import RepoCache

PLAN = "docs/plans/2026-11-24-login-fix.review.md"


def g(*a, cwd=None, env=None, inp=None):
    return subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True, input=inp).stdout.strip()


_IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@pytest.fixture
def git_env():
    return dict(_IDENT)


@pytest.fixture
def origin(tmp_path):
    env = {**os.environ, **_IDENT}
    bare = str(tmp_path / "origin.git")
    work = str(tmp_path / "seed")
    g("init", "-q", "--bare", "-b", "main", bare, env=env)
    g("init", "-q", "-b", "main", work, env=env)
    with open(os.path.join(work, "README.md"), "w") as f:
        f.write("hello\n")
    os.makedirs(os.path.join(work, "docs", "plans"))
    open(os.path.join(work, "docs", "plans", ".keep"), "w").close()
    g("add", ".", cwd=work, env=env)
    g("commit", "-qm", "init", cwd=work, env=env)
    g("push", "-q", bare, "main", cwd=work, env=env)
    return bare


@pytest.fixture
def setup(tmp_path, origin, git_env):
    env = {**os.environ, **git_env}
    cache = RepoCache(str(tmp_path / "c.git"), origin)
    cache.fetch()
    work = str(tmp_path / "w")
    g("clone", "-q", origin, work, env=env)
    os.makedirs(os.path.join(work, "docs/plans"), exist_ok=True)
    with open(os.path.join(work, PLAN), "w") as f:
        f.write("# Login fix\n\n- [ ] T1 redirect\n")
    g("add", ".", cwd=work, env=env)
    g("commit", "-qm", "plan", cwd=work, env=env)
    g("push", "-q", origin, "main", cwd=work, env=env)
    cache.fetch()
    gate = g("-C", cache.path, "rev-parse", "main")
    g("-C", cache.path, "update-ref", files.gate_ref("t_1"), gate)
    return cache, gate, env, work, tmp_path


def test_refs():
    assert files.gate_ref("t_1") == "refs/runs/t_1/gate"
    assert files.edits_ref("t_1") == "refs/runs/t_1/edits"


def test_list_and_read(setup):
    cache, gate, *_ = setup
    assert files.list_files(cache, gate) == ["README.md", "docs/plans/.keep", PLAN]
    assert files.read_file(cache, gate, PLAN) == "# Login fix\n\n- [ ] T1 redirect\n"


def test_write_file_stacks(setup):
    cache, gate, *_ = setup
    s1, c1 = files.write_file(cache, "t_1", gate, PLAN, "# Login fix v2\n")
    assert c1 == [PLAN]
    s2, c2 = files.write_file(cache, "t_1", gate, "docs/plans/other.md", "x\n")
    assert g("-C", cache.path, "rev-parse", s2 + "^") == s1
    assert g("-C", cache.path, "rev-parse", files.edits_ref("t_1")) == s2
    assert c2 == sorted([PLAN, "docs/plans/other.md"])
    assert files.read_file(cache, s2, PLAN) == "# Login fix v2\n"


def test_bundle_roundtrip(setup):
    cache, gate, env, work, tmp = setup
    out = files.make_bundle(cache, "t_1", str(tmp / "b" / "gate.bundle"))
    clone = str(tmp / "clone")
    g("clone", "-q", "--no-checkout", out, clone, env=env)
    g("fetch", "-q", out, files.gate_ref("t_1") + ":refs/g", cwd=clone, env=env)
    g("checkout", "-qb", "edits", gate, cwd=clone, env=env)
    with open(os.path.join(clone, PLAN), "w") as f:
        f.write("# edited\n")
    g("commit", "-qam", "edit", cwd=clone, env=env)
    new = g("rev-parse", "HEAD", cwd=clone)
    nb = str(tmp / "new.bundle")
    g("bundle", "create", nb, "edits", cwd=clone, env=env)
    sha, ch = files.accept_bundle(cache, "t_1", gate, nb)
    assert sha == new and ch == [PLAN]
    assert g("-C", cache.path, "rev-parse", files.edits_ref("t_1")) == new


def test_rewrite_refused(setup):
    cache, gate, env, work, tmp = setup
    # a root commit unrelated to the gate, as a bundle
    other = str(tmp / "other")
    g("init", "-q", "-b", "main", other, env=env)
    open(os.path.join(other, "a"), "w").write("a\n")
    g("add", ".", cwd=other, env=env)
    g("commit", "-qm", "a", cwd=other, env=env)
    nb = str(tmp / "o.bundle")
    g("bundle", "create", nb, "main", cwd=other, env=env)
    with pytest.raises(files.NotFastForward):
        files.accept_bundle(cache, "t_1", gate, nb)
    g("-C", cache.path, "fetch", "-q", other, "main:refs/heads/rewrite")
    rew = g("-C", cache.path, "rev-parse", "rewrite")
    with pytest.raises(files.NotFastForward, match="not a fast-forward"):
        files.accept_remote(cache, "t_1", gate, rew)
    with pytest.raises(files.NotFastForward, match="no new commits"):
        files.accept_remote(cache, "t_1", gate, gate)
    p = subprocess.run(["git", "-C", cache.path, "rev-parse", "--verify", "-q",
                        files.edits_ref("t_1")], capture_output=True)
    assert p.returncode != 0


def test_accept_remote_ok(setup):
    cache, gate, env, work, tmp = setup
    with open(os.path.join(work, PLAN), "w") as f:
        f.write("# r\n")
    g("commit", "-qam", "r", cwd=work, env=env)
    g("push", "-q", cache.repo, "main", cwd=work, env=env)
    new = g("rev-parse", "HEAD", cwd=work)
    sha, ch = files.accept_remote(cache, "t_1", gate, new)
    assert sha == new and ch == [PLAN]


def test_read_missing(setup):
    cache, gate, *_ = setup
    with pytest.raises(NotFound):
        files.read_file(cache, gate, "nope.md")


@pytest.mark.parametrize("p", ["../x", "/etc/x", "a/../../x", ".git/config", "a/.git/b"])
def test_path_escape(setup, p):
    cache, gate, *_ = setup
    with pytest.raises(ValueError):
        files.write_file(cache, "t_1", gate, p, "x")


def _ref_exists(cache, ref):
    return subprocess.run(["git", "-C", cache.path, "rev-parse", "--verify", "-q", ref],
                          capture_output=True).returncode == 0


def test_rejected_bundle_leaves_no_refs(setup):
    cache, gate, env, work, tmp = setup
    other = str(tmp / "other2")
    g("init", "-q", "-b", "main", other, env=env)
    open(os.path.join(other, "a"), "w").write("a\n")
    g("add", ".", cwd=other, env=env)
    g("commit", "-qm", "a", cwd=other, env=env)
    nb = str(tmp / "o2.bundle")
    g("bundle", "create", nb, "main", cwd=other, env=env)
    with pytest.raises(files.NotFastForward):
        files.accept_bundle(cache, "t_1", gate, nb)
    assert not _ref_exists(cache, files.edits_ref("t_1"))
    assert not _ref_exists(cache, "refs/runs/t_1/incoming")
    assert _ref_exists(cache, files.gate_ref("t_1"))


def test_write_file_leaks_no_index(setup):
    cache, gate, *_ = setup
    files.write_file(cache, "t_1", gate, PLAN, "# x\n")
    assert not os.path.exists(os.path.join(cache.path, "index"))


def test_gate_ref_survives_accept_remote(setup):
    cache, gate, env, work, tmp = setup
    with open(os.path.join(work, PLAN), "w") as f:
        f.write("# r2\n")
    g("commit", "-qam", "r2", cwd=work, env=env)
    g("push", "-q", cache.repo, "main", cwd=work, env=env)
    files.accept_remote(cache, "t_1", gate, g("rev-parse", "HEAD", cwd=work))
    assert g("-C", cache.path, "rev-parse", files.gate_ref("t_1")) == gate


def test_drop_refs_frees_run_ref(setup):
    cache, gate, *_ = setup
    files.write_file(cache, "t_1", gate, PLAN, "# x\n")
    g("-C", cache.path, "update-ref", "refs/runs/t_1/incoming", gate)
    files.drop_refs(cache, "t_1")
    for r in (files.gate_ref("t_1"), files.edits_ref("t_1"), "refs/runs/t_1/incoming"):
        assert not _ref_exists(cache, r)
    g("-C", cache.path, "update-ref", "refs/runs/t_1", gate)
    assert g("-C", cache.path, "rev-parse", "refs/runs/t_1") == gate
    files.drop_refs(cache, "t_1")
