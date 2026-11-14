"""Command-line entry point."""

from __future__ import annotations

import json
import os
import sys

from slcloud import learning
from slcloud.connect import AGENTS, connect_command
from slcloud.errors import Rejected

USAGE = "usage: slcloud init|token create|key set <provider>|connect <agent>|sync|serve|worker"


def _http(method: str, url: str, token: str, body: dict | None):
    import httpx

    r = httpx.request(method, url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data


def _state_path(env: dict) -> str:
    return os.path.join(env.get("HOME") or os.path.expanduser("~"), ".config", "slcloud", "sync.json")


def _load_state(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(path: str, state: dict) -> None:
    # write a temp file then rename: a torn write must never wipe every URL's offset
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _client(env: dict, err) -> tuple[str, str] | None:
    url, token = env.get("SLCLOUD_URL", ""), env.get("SLCLOUD_TOKEN", "")
    if not url or not token:
        err.write("set SLCLOUD_URL and SLCLOUD_TOKEN\n")
        return None
    return url.rstrip("/"), token


def _sync(env, http, out, err) -> int:
    c = _client(env, err)
    if not c:
        return 1
    url, token = c
    home = env.get("HOME") or os.path.expanduser("~")
    outbox = env.get("SLCLOUD_OUTBOX") or os.path.join(home, ".superlite", "learning", "outbox.jsonl")
    path = _state_path(env)
    state = _load_state(path)
    rows, new_offset = learning.outbox_rows(outbox, int(state.get(url, 0)))
    good = []
    for r in rows:
        try:
            learning.accept_row(r)
        except (Rejected, ValueError):
            continue  # never log row content
        good.append(r)
    skipped = len(rows) - len(good)
    if skipped:
        err.write(f"skipped {skipped} rows with fields not allowed\n")
    rows = good
    if not rows:
        if skipped:
            state[url] = new_offset
            _save_state(path, state)
        out.write("nothing to sync\n")
        return 0
    status, _ = http("POST", f"{url}/api/learn/rows", token, {"rows": rows})
    if status != 200:
        err.write(f"sync failed: HTTP {status}\n")
        return 1
    state[url] = new_offset
    _save_state(path, state)
    out.write(f"synced {len(rows)} rows\n")
    return 0


def _key_set(provider, env, http, stdin, out, err) -> int:
    c = _client(env, err)
    if not c:
        return 1
    url, token = c
    key = stdin.read().strip()
    if not key:
        err.write("no key on stdin\n")
        return 1
    status, data = http("PUT", f"{url}/api/keys/{provider}", token, {"key": key})
    if status != 200:
        err.write(f"key not stored: HTTP {status}\n")
        return 1
    out.write(f"{provider} key ending {data.get('last4', '????')} stored; works: {data.get('works')}\n")
    return 0


def _server(cmd: list[str], env, out) -> int:
    from slcloud import app
    from slcloud.config import load_settings

    settings = load_settings(dict(env))
    if cmd[0] == "init":
        ws, token = app.init_workspace(settings, cmd[1] if len(cmd) > 1 else "team")
        out.write(f"workspace {ws}\nadmin token {token}\n")
        return 0
    if cmd[0] == "token":
        from slcloud import auth
        from slcloud.store import Store

        if len(cmd) < 4 or cmd[1] != "create":
            return 2
        out.write(auth.create_token(Store(settings.db_path), cmd[2], cmd[3]) + "\n")
        return 0
    if cmd[0] == "serve":
        import uvicorn

        uvicorn.run(app.build(settings), host="0.0.0.0", port=8787)
        return 0
    app.run_worker(settings)
    return 0


def main(argv=None, *, http=None, env=None, stdin=None, out=None, err=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    stdin, out, err = stdin or sys.stdin, out or sys.stdout, err or sys.stderr
    http = http or _http
    cmd = argv[0] if argv else ""
    try:
        if cmd == "connect" and len(argv) == 2 and argv[1] in AGENTS:
            c = _client(env, err)
            if not c:
                return 1
            out.write(connect_command(argv[1], c[0], c[1]) + "\n")
            return 0
        if cmd == "sync" and len(argv) == 1:
            return _sync(env, http, out, err)
        if cmd == "key" and len(argv) == 3 and argv[1] == "set":
            return _key_set(argv[2], env, http, stdin, out, err)
        if cmd in ("init", "serve", "worker") or (cmd == "token" and len(argv) == 4 and argv[1] == "create"):
            return _server(argv, env, out)
    except Exception as e:  # never echo secrets: report the type only
        err.write(f"error: {type(e).__name__}\n")
        return 1
    err.write(USAGE + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
