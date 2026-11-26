"""Read and write run files in the bare repo cache. Never touches a work tree."""
import os
import subprocess
import tempfile

from slcloud.errors import NotFound

_IDENT = {"GIT_AUTHOR_NAME": "superlite", "GIT_AUTHOR_EMAIL": "superlite@localhost",
          "GIT_COMMITTER_NAME": "superlite", "GIT_COMMITTER_EMAIL": "superlite@localhost"}


class NotFastForward(Exception):
    pass


def gate_ref(task_id: str) -> str:
    return f"refs/runs/{task_id}/gate"


def edits_ref(task_id: str) -> str:
    return f"refs/runs/{task_id}/edits"


def _incoming_ref(task_id: str) -> str:
    return f"refs/runs/{task_id}/incoming"


def _git(cache, args, env=None, inp=None, check=True):
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0", **_IDENT)
    if env:
        e.update(env)
    p = subprocess.run(["git", "-C", cache.path, *args], env=e, input=inp,
                       capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()[:300]}")
    return p


def _has(cache, ref):
    return _git(cache, ["rev-parse", "--verify", "-q", ref + "^{commit}"],
                check=False).returncode == 0


def list_files(cache, sha: str) -> list[str]:
    out = _git(cache, ["ls-tree", "-r", "--name-only", sha]).stdout
    return [ln for ln in out.splitlines() if ln]


def read_file(cache, sha: str, path: str) -> str:
    p = _git(cache, ["show", f"{sha}:{path}"], check=False)
    if p.returncode != 0:
        raise NotFound(path)
    return p.stdout


def changed(cache, base: str, sha: str) -> list[str]:
    out = _git(cache, ["diff", "--name-only", base, sha]).stdout
    return sorted(ln for ln in out.splitlines() if ln)


def make_bundle(cache, task_id: str, out: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    return cache.bundle(gate_ref(task_id), out)


def _check(cache, gate_sha, sha):
    if sha == gate_sha:
        raise NotFastForward("no new commits")
    if _git(cache, ["merge-base", "--is-ancestor", gate_sha, sha], check=False).returncode != 0:
        raise NotFastForward("not a fast-forward of the gate commit")


def _set_edits(cache, task_id, gate_sha, sha, precheck=None, record=None):
    """Move the edits ref to sha and record it, as one step. The caller holds cache.lock().

    precheck() may raise to refuse the write before anything changes; if record() raises, the
    previous edits ref is restored so ref and database row never disagree."""
    _check(cache, gate_sha, sha)
    if precheck is not None:
        precheck()
    ref = edits_ref(task_id)
    old = _git(cache, ["rev-parse", ref]).stdout.strip() if _has(cache, ref) else None
    _git(cache, ["update-ref", ref, sha])
    try:
        names = changed(cache, gate_sha, sha)
        if record is not None:
            record(sha, names)
    except BaseException:
        if old:
            _git(cache, ["update-ref", ref, old], check=False)
        else:
            _git(cache, ["update-ref", "-d", ref], check=False)
        raise
    return sha, names


def accept_bundle(cache, task_id: str, gate_sha: str, bundle_path: str,
                  precheck=None, record=None) -> tuple[str, list[str]]:
    heads = _git(cache, ["bundle", "list-heads", bundle_path]).stdout.split()
    refs = heads[1::2]
    if len(refs) != 1:
        raise NotFastForward("bundle must hold exactly one head")
    tmp = _incoming_ref(task_id)
    with cache.lock():
        try:
            _git(cache, ["fetch", "-q", "--no-tags", bundle_path, f"+{refs[0]}:{tmp}"])
            sha = _git(cache, ["rev-parse", tmp]).stdout.strip()
            return _set_edits(cache, task_id, gate_sha, sha, precheck, record)
        finally:
            _git(cache, ["update-ref", "-d", tmp], check=False)


def accept_remote(cache, task_id: str, gate_sha: str, sha: str,
                  precheck=None, record=None) -> tuple[str, list[str]]:
    with cache.lock():
        cache.fetch()
        if _git(cache, ["cat-file", "-e", sha + "^{commit}"], check=False).returncode != 0:
            raise NotFastForward("unknown commit")
        return _set_edits(cache, task_id, gate_sha, sha, precheck, record)


def _clean(path):
    if not path or os.path.isabs(path) or "\\" in path:
        raise ValueError("bad path")
    parts = path.split("/")
    if any(p in ("", ".", "..", ".git") for p in parts):
        raise ValueError("bad path")
    return path


def _mode(cache, parent, path):
    out = _git(cache, ["ls-tree", parent, "--", path]).stdout
    for ln in out.splitlines():
        meta, _, name = ln.partition("\t")
        if name == path:
            mode = meta.split()[0]
            if mode in ("100644", "100755"):
                return mode
            raise ValueError(f"not a regular file: {path}")
    return "100644"


def write_file(cache, task_id: str, gate_sha: str, path: str, text: str,
               precheck=None, record=None) -> tuple[str, list[str]]:
    _clean(path)
    with cache.lock():
        if precheck is not None:
            precheck()
        parent = gate_sha
        if _has(cache, edits_ref(task_id)):
            parent = _git(cache, ["rev-parse", edits_ref(task_id)]).stdout.strip()
        mode = _mode(cache, parent, path)
        blob = _git(cache, ["hash-object", "-w", "--stdin"], inp=text).stdout.strip()
        with tempfile.TemporaryDirectory() as td:
            env = {"GIT_INDEX_FILE": os.path.join(td, "index")}
            _git(cache, ["read-tree", parent], env=env)
            p = _git(cache, ["update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}"],
                     env=env, check=False)
            if p.returncode != 0:
                raise ValueError(f"cannot write {path}: a parent is not a directory")
            tree = _git(cache, ["write-tree"], env=env).stdout.strip()
        new = _git(cache, ["commit-tree", tree, "-p", parent, "-m", f"edit {path}"]).stdout.strip()
        return _set_edits(cache, task_id, gate_sha, new, None, record)


def drop_refs(cache, task_id: str) -> None:
    for ref in (gate_ref(task_id), edits_ref(task_id), _incoming_ref(task_id)):
        cache.drop_ref(ref)
