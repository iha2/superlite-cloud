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
