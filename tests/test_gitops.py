import os
import subprocess

import pytest

from slcloud import gitops
from slcloud.errors import Rejected


def git(*args, cwd=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    seed = tmp_path / "seed"
    bare = tmp_path / "r.git"
    git("init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("a")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "init", cwd=seed)
    git("clone", "-q", "--bare", str(seed), str(bare))
    return str(bare)


def test_clone_at_sha(remote, tmp_path):
    sha = gitops.clone_at(remote, "HEAD", str(tmp_path / "w"))
    assert len(sha) == 40 and sha == git("rev-parse", "HEAD", cwd=tmp_path / "w")
    assert (tmp_path / "w" / "a.txt").exists()


def test_bad_base(remote, tmp_path):
    with pytest.raises(Rejected):
        gitops.clone_at(remote, "nope-branch", str(tmp_path / "w"))
    with pytest.raises(Rejected):
        gitops.clone_at(remote, "--upload-pack=x", str(tmp_path / "w2"))


def test_repo_arg_injection(tmp_path):
    with pytest.raises(Rejected):
        gitops.clone_at("--upload-pack=x", "HEAD", str(tmp_path / "w"))


def _work(remote, tmp_path):
    w = str(tmp_path / "w")
    gitops.clone_at(remote, "HEAD", w)
    (tmp_path / "w" / "b.txt").write_text("b")
    git("update-ref", "refs/superlite/x", "HEAD", cwd=w)
    return w


def test_superlite_refs_pushed(remote, tmp_path):
    w = _work(remote, tmp_path)
    hb = gitops.hand_back(w, "slcloud/t_1", str(tmp_path / "out"))
    assert hb.pushed and hb.bundle is None and len(hb.sha) == 40
    assert hb.fetch == "git fetch origin slcloud/t_1 'refs/superlite/*:refs/superlite/*'"
    assert git("rev-parse", "refs/superlite/x", cwd=remote)
    assert git("rev-parse", "refs/heads/slcloud/t_1", cwd=remote) == hb.sha


def test_bundle_fallback(remote, tmp_path):
    w = _work(remote, tmp_path)
    git("remote", "set-url", "origin", str(tmp_path / "missing.git"), cwd=w)
    hb = gitops.hand_back(w, "slcloud/t_1", str(tmp_path / "out"))
    assert not hb.pushed and hb.bundle.endswith("t_1.bundle")
    assert hb.fetch == "git fetch <bundle-url> slcloud/t_1"
    git("bundle", "verify", hb.bundle, cwd=w)


def test_push_false_bundles(remote, tmp_path):
    w = _work(remote, tmp_path)
    hb = gitops.hand_back(w, "slcloud/t_2", str(tmp_path / "out"), push=False)
    assert not hb.pushed and os.path.exists(hb.bundle)


def test_bad_branch(remote, tmp_path):
    w = _work(remote, tmp_path)
    with pytest.raises(Rejected):
        gitops.hand_back(w, "--delete", str(tmp_path / "out"))


def test_env_not_committed_and_relative_out(remote, tmp_path, monkeypatch):
    w = _work(remote, tmp_path)
    (tmp_path / "w" / ".env").write_text("SECRET=1")
    monkeypatch.chdir(tmp_path)
    hb = gitops.hand_back(w, "slcloud/t_3", "relout", push=False)
    assert hb.bundle == str(tmp_path / "relout" / "t_3.bundle")
    assert os.path.exists(hb.bundle)
    assert ".env" not in git("ls-tree", "-r", "--name-only", "HEAD", cwd=w)


# ---- safe_hand_back (fake RepoCache) ----
import contextlib
import sys
import types

SHA = "3f9e2c1" + "0" * 33


class FakeCache:
    def __init__(self, branches, pushes, fetch_err=False):
        self.branches, self.pushes, self.fetch_err = set(branches), list(pushes), fetch_err
        self.pushed, self.dropped, self.bundled, self.fetched = [], [], [], []

    @contextlib.contextmanager
    def lock(self):
        yield

    def fetch(self):
        return set(self.branches)

    def fetch_from(self, src, branch, into):
        if self.fetch_err:
            raise RuntimeError("boom")
        self.fetched.append(branch)
        return SHA

    def push(self, ref, branch, expect=""):
        self.pushed.append(branch)
        self.expects = getattr(self, "expects", []) + [expect]
        return self.pushes.pop(0)

    def tip(self, branch):
        return "abc123"

    def bundle(self, ref, out):
        self.bundled.append(out)
        return out

    def drop_ref(self, ref):
        self.dropped.append(ref)


@pytest.fixture(autouse=True)
def _names(monkeypatch):
    try:
        import slcloud.names as n
        n.free_name("a", {"a"})
        return
    except Exception:
        pass

    def free_name(base, taken):
        i = 2
        while f"{base}_{i}" in taken:
            i += 1
        return f"{base}_{i}" if base in taken else base
    mod = types.ModuleType("slcloud.names")
    mod.free_name = free_name
    mod.slug = lambda n: n.replace("_", "-")
    monkeypatch.setitem(sys.modules, "slcloud.names", mod)


def _hb(cache, name="login_fix", own=(), tmp=None, live=None, rename=None):
    calls, logs = [], []

    def default_rename(a, b):
        calls.append((a, b))
    hb = gitops.safe_hand_back(cache, "/work/t_1", name, "t_1", set(own),
                               rename or default_rename, lambda: calls.append("commit"),
                               logs.append, str(tmp), live)
    return hb, calls, logs


def test_branch_free(tmp_path):
    c = FakeCache({"main"}, [True])
    hb, calls, logs = _hb(c, tmp=tmp_path)
    assert hb == gitops.HandBack("superlite/sessions/login-fix", SHA, True, None,
                                 "git fetch origin superlite/sessions/login-fix")
    assert calls == ["commit"] and not logs


def test_clash_renamed(tmp_path):
    c = FakeCache({"main", "superlite/sessions/login-fix", "superlite/sessions/login-fix-2"}, [True])
    hb, calls, logs = _hb(c, tmp=tmp_path)
    assert ("login_fix", "login_fix_3") in calls
    assert logs == ["renamed login_fix to login_fix_3: branch taken on origin"]
    assert hb.branch == "superlite/sessions/login-fix-3"


def test_own_branch_not_clash(tmp_path):
    c = FakeCache({"superlite/sessions/login-fix"}, [True])
    hb, calls, logs = _hb(c, own={"login_fix"}, tmp=tmp_path)
    assert calls == ["commit"] and hb.branch == "superlite/sessions/login-fix"


def test_push_race(tmp_path):
    c = FakeCache({"main"}, [False, True])

    def fetch2():
        return {"main"} if not c.pushed else {"main", "superlite/sessions/login-fix"}
    c.fetch = fetch2
    hb, calls, logs = _hb(c, tmp=tmp_path)
    assert [x for x in calls if x != "commit"] == [("login_fix", "login_fix_2")]
    assert hb.pushed and hb.branch == "superlite/sessions/login-fix-2"
    assert c.pushed == ["superlite/sessions/login-fix", "superlite/sessions/login-fix-2"]


def test_bundle_fallback(tmp_path):
    c = FakeCache({"main"}, [False, False])
    hb, _, _ = _hb(c, tmp=tmp_path / "b")
    assert not hb.pushed and hb.bundle and hb.bundle.endswith("t_1.bundle")
    assert c.bundled == [hb.bundle]


def test_ref_dropped(tmp_path):
    c = FakeCache({"main"}, [True], fetch_err=True)
    with pytest.raises(RuntimeError):
        _hb(c, tmp=tmp_path)
    assert c.dropped == ["refs/runs/t_1"]


def test_bundle_named_by_task_id_and_url_placeholder(tmp_path):
    c = FakeCache({"main"}, [False, False])
    hb, _, _ = _hb(c, tmp=tmp_path / "b")
    assert os.path.basename(hb.bundle) == "t_1.bundle"
    assert hb.fetch == "git fetch <bundle-url> refs/runs/t_1:refs/heads/superlite/sessions/login-fix"
    assert "/work" not in hb.fetch and str(tmp_path) not in hb.fetch


def test_origin_outage_still_commits_and_bundles(tmp_path):
    c = FakeCache({"main"}, [False, False])

    def down():
        raise RuntimeError("git fetch failed: origin unreachable")
    c.fetch = down
    hb, calls, logs = _hb(c, tmp=tmp_path / "b")
    assert calls == ["commit"]  # the run was committed despite the outage
    assert c.fetched == ["superlite/sessions/login-fix"]  # and imported into the cache
    assert not hb.pushed and hb.bundle and c.bundled == [hb.bundle]
    assert any("origin unreachable" in m for m in logs)


def test_replacement_avoids_other_live_runs(tmp_path):
    c = FakeCache({"main", "superlite/sessions/login-fix"}, [True])
    hb, calls, _ = _hb(c, tmp=tmp_path, live=lambda: {"login_fix_2"})
    assert ("login_fix", "login_fix_3") in calls
    assert hb.branch == "superlite/sessions/login-fix-3"


def test_reserved_name_from_rename_is_used(tmp_path):
    c = FakeCache({"main", "superlite/sessions/login-fix"}, [True])
    hb, _, logs = _hb(c, tmp=tmp_path, rename=lambda a, b: "login_fix_9")
    assert hb.branch == "superlite/sessions/login-fix-9"
    assert logs == ["renamed login_fix to login_fix_9: branch taken on origin"]


def test_new_branch_push_expects_absent_and_owned_expects_tip(tmp_path):
    c = FakeCache({"main"}, [True])
    _hb(c, tmp=tmp_path)
    assert c.expects == [""]
    c = FakeCache({"superlite/sessions/login-fix"}, [True])
    _hb(c, own={"login_fix"}, tmp=tmp_path)
    assert c.expects == ["abc123"]
