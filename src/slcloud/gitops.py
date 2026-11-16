"""Git clone and hand-back helpers."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from slcloud.errors import Rejected

_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "file:git:http:https:ssh",
    "GIT_AUTHOR_NAME": "superlite",
    "GIT_AUTHOR_EMAIL": "superlite@localhost",
    "GIT_COMMITTER_NAME": "superlite",
    "GIT_COMMITTER_EMAIL": "superlite@localhost",
}
_SAFE = re.compile(r"^[A-Za-z0-9_./@^~+-]+$")


@dataclass
class HandBack:
    branch: str
    sha: str
    pushed: bool
    bundle: str | None
    fetch: str


def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)


def _check_arg(value: str, what: str) -> None:
    if not value or value.startswith("-"):
        raise Rejected(f"invalid {what}")
    if what != "repo" and not _SAFE.match(value):
        raise Rejected(f"invalid {what}")
    if "\x00" in value or "\n" in value:
        raise Rejected(f"invalid {what}")


def clone_at(repo: str, base: str, dest: str) -> str:
    _check_arg(repo, "repo")
    _check_arg(base, "base")
    r = _git(["clone", "--quiet", "--no-checkout", "--", repo, dest])
    if r.returncode != 0:
        raise Rejected("clone failed")
    r = _git(["checkout", "--quiet", "--detach", f"{base}^{{commit}}", "--"], cwd=dest)
    if r.returncode != 0:
        raise Rejected(f"unknown base: {base}")
    return _git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()


def hand_back(workdir: str, branch: str, out_dir: str, push: bool = True) -> HandBack:
    _check_arg(branch, "branch")
    if _git(["check-ref-format", "--branch", branch]).returncode != 0:
        raise Rejected("invalid branch")
    if _git(["checkout", "--quiet", "-B", branch], cwd=workdir).returncode != 0:
        raise Rejected("cannot create branch")
    _git(["add", "-A", "--", ".", ":(exclude,glob)**/.env", ":(exclude,glob)**/.env.*"],
         cwd=workdir)
    if _git(["status", "--porcelain"], cwd=workdir).stdout.strip():
        r = _git(["commit", "--quiet", "-m", f"superlite: {branch}"], cwd=workdir)
        if r.returncode != 0:
            raise Rejected("commit failed")
    sha = _git(["rev-parse", "HEAD"], cwd=workdir).stdout.strip()
    if push:
        r = _git(["push", "--quiet", "origin", "--", f"refs/heads/{branch}:refs/heads/{branch}",
                  "refs/superlite/*:refs/superlite/*"], cwd=workdir)
        if r.returncode == 0:
            return HandBack(branch, sha, True, None,
                            f"git fetch origin {branch} 'refs/superlite/*:refs/superlite/*'")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, branch.rsplit("/", 1)[-1] + ".bundle")
    refs = _git(["for-each-ref", "--format=%(refname)", "refs/superlite"], cwd=workdir).stdout.split()
    r = _git(["bundle", "create", path, f"refs/heads/{branch}", *refs], cwd=workdir)
    if r.returncode != 0:
        raise Rejected("bundle failed")
    return HandBack(branch, sha, False, path, f"git fetch <bundle-url> {branch}")


_PREFIX = "superlite/sessions/"


def _taken(cache) -> set[str]:
    return {b[len(_PREFIX):].replace("-", "_")
            for b in cache.fetch() if b.startswith(_PREFIX)}


def safe_hand_back(cache, run_dir: str, name: str, task_id: str, own: set[str],
                   rename: Callable[[str, str], None], commit: Callable[[], None],
                   log: Callable[[str], None], bundle_dir: str) -> HandBack:
    """Hand a run back through the shared repo cache without clobbering others."""
    from slcloud.names import free_name, slug

    ref = f"refs/runs/{task_id}"

    def clash(cur: str) -> str:
        taken = _taken(cache)
        if cur in taken and cur not in own:
            new = free_name(cur, taken)
            rename(cur, new)
            log(f"renamed {cur} to {new}: branch taken on origin")
            return new
        return cur

    try:
        with cache.lock():
            name = clash(name)
            commit()
            branch = _PREFIX + slug(name)
            sha = cache.fetch_from(run_dir, branch, into=ref)
            ok = cache.push(ref, branch)
            if not ok:
                new = clash(name)
                if new != name:
                    name = new
                    branch = _PREFIX + slug(name)
                    sha = cache.fetch_from(run_dir, branch, into=ref)
                ok = cache.push(ref, branch)
            if ok:
                return HandBack(branch, sha, True, None, f"git fetch origin {branch}")
            os.makedirs(bundle_dir, exist_ok=True)
            out = os.path.join(bundle_dir, f"{task_id}.bundle")
            path = cache.bundle(ref, out)
            return HandBack(branch, sha, False, path,
                            f"git fetch <bundle-url> {ref}:refs/heads/{branch}")
    finally:
        cache.drop_ref(ref)
