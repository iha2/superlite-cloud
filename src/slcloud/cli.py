"""Command-line entry point."""

from __future__ import annotations

import json
import os
import socket
import sys
import time

from slcloud import learning
from slcloud.connect import AGENTS, connect_command
from slcloud.errors import HandleTaken, Rejected

USAGE = ("usage: slcloud init [name] --member H|token create <ws> <role> --member H [--email E]|member list|rename|email|role"
         "|invite H [--email E]|backup FILE|restore FILE|key set <provider>|connect <agent>|sync|serve|worker")


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


def _opts(args: list[str], allowed: tuple[str, ...]) -> tuple[list[str], dict] | None:
    pos, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if a[2:] not in allowed or i + 1 >= len(args) or a[2:] in opts:
                return None
            opts[a[2:]] = args[i + 1]
            i += 2
        else:
            pos.append(a)
            i += 1
    return pos, opts


def _db_path(env) -> str:
    return f"{env.get('SLCLOUD_DATA', '/data')}/slcloud.db"


def _port_open(host: str = "127.0.0.1", port: int = 8787) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _workspace(store, ref: str | None) -> str | None:
    if ref:
        rows = store.query("SELECT id FROM workspaces WHERE id=? OR name=?", (ref, ref))
    else:
        rows = store.query("SELECT id FROM workspaces")
    return rows[0]["id"] if len(rows) == 1 else None


def _admin(argv: list[str], env, out, err, server_running) -> int:
    from slcloud import accounts, app, auth, backup, members
    from slcloud.config import load_settings
    from slcloud.store import Store

    cmd, rest = argv[0], argv[1:]
    if cmd == "backup" or cmd == "restore":
        if len(rest) != 1:
            return _usage(err)
        db = _db_path(env)
        try:
            if cmd == "backup":
                backup.backup(db, rest[0])
                out.write("backup written\n")
            else:
                backup.restore(db, rest[0], server_running)
                out.write("restored\n")
        except backup.Refused as e:
            err.write(f"refused: {e}\n")
            return 2
        return 0
    if cmd == "init":
        o = _opts(rest, ("member",))
        if o is None or len(o[0]) > 1:
            return _usage(err)
        settings = load_settings(dict(env))
        try:
            token, invite = app.init_workspace(settings, o[0][0] if o[0] else "team", member=o[1].get("member"))
        except HandleTaken:
            err.write("handle taken\n")
            return 2
        out.write(f"token: {token}\ninvite: {invite}\n")
        return 0
    if cmd == "token":
        o = _opts(rest[1:], ("member", "email"))
        if rest[:1] != ["create"] or o is None or len(o[0]) != 2 or "member" not in o[1]:
            return _usage(err)
        ref, role = o[0]
        store = Store(_db_path(env))
        handle, email = o[1]["member"], o[1].get("email")
        if role not in auth.ROLES:
            err.write(f"role must be one of {', '.join(auth.ROLES)}\n")
            return 2
        ws = _workspace(store, ref)
        if ws is None:
            err.write("no such workspace\n")
            return 2
        m = members.member_by_handle(store, ws, handle)
        if m is not None:
            if m["role"] != role:
                err.write(f"{handle} is {m['role']}, not {role}\n")
                return 2
            mid = m["id"]
        else:
            if not email:
                err.write(f"{handle} is new: pass --email\n")
                return 2
            try:
                mid = members.create_member(store, ws, handle, email, role)
            except HandleTaken:
                err.write("handle taken\n")
                return 2
        out.write(auth.create_token(store, ws, role, mid) + "\n")
        return 0
    if cmd == "member":
        sub = rest[:1]
        o = _opts(rest[1:], ("workspace",))
        if not sub or o is None or sub[0] not in ("list", "rename", "email", "role"):
            return _usage(err)
        need = 0 if sub[0] == "list" else 2
        if len(o[0]) != need:
            return _usage(err)
        store = Store(_db_path(env))
        ws = _workspace(store, o[1].get("workspace"))
        if ws is None:
            err.write("pass --workspace\n")
            return 2
        if sub[0] == "list":
            for m in members.list_members(store, ws):
                out.write(f"{m['handle']}  {m['email'] or '-'}  {m['role']}\n")
            return 0
        m = members.member_by_handle(store, ws, o[0][0])
        if m is None:
            err.write("no such member\n")
            return 2
        try:
            {"rename": members.rename, "email": members.set_email, "role": members.set_role}[sub[0]](
                store, m["id"], o[0][1])
        except ValueError as e:
            err.write(f"error: {e}\n")
            return 2
        return 0
    if cmd == "invite":
        o = _opts(rest, ("email", "workspace"))
        if o is None or len(o[0]) != 1:
            return _usage(err)
        store = Store(_db_path(env))
        ws = _workspace(store, o[1].get("workspace"))
        m = members.member_by_handle(store, ws, o[0][0]) if ws else None
        if m is None:
            err.write("no such member\n")
            return 2
        if o[1].get("email"):
            members.set_email(store, m["id"], o[1]["email"])
        out.write(f"invite: {accounts.create_invite(store, m['id'], time.time())}\n")
        return 0
    return _usage(err)


def _usage(err) -> int:
    err.write(USAGE + "\n")
    return 2


def _server(cmd: list[str], env, out) -> int:
    from slcloud import app
    from slcloud.config import load_settings

    settings = load_settings(dict(env))
    if cmd[0] == "serve":
        import uvicorn

        uvicorn.run(app.build(settings), host="0.0.0.0", port=8787)
        return 0
    app.run_worker(settings)
    return 0


def main(argv=None, *, http=None, env=None, stdin=None, out=None, err=None, server_running=None) -> int:
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
        if cmd in ("init", "member", "invite", "backup", "restore") or (cmd == "token" and argv[1:2] == ["create"]):
            return _admin(argv, env, out, err, server_running or _port_open)
        if cmd in ("serve", "worker") and len(argv) == 1:
            return _server(argv, env, out)
    except Exception as e:  # never echo secrets: report the type only
        err.write(f"error: {type(e).__name__}\n")
        return 1
    err.write(USAGE + "\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
