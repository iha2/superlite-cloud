"""Remote MCP tools for submitting and following tasks."""

from __future__ import annotations

import functools
import json

from slcloud import auth, files, filestore, learning, repocache, runs
from slcloud.errors import BadTransition, NotFound, Rejected

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

    def _new() -> FastMCP:
        return FastMCP("slcloud", stateless_http=True, json_response=True)

except ModuleNotFoundError:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

    def _new() -> FastMCP:
        return FastMCP("slcloud")


def tools(engine, store, settings, whoami) -> dict:
    def guard(fn):
        @functools.wraps(fn)
        def run(*a, **kw):
            try:
                return fn(*a, **kw)
            except NotFound:
                return {"error": "not found"}
            except runs.NoSuchMember:
                return {"error": "no such member"}
            except (BadTransition, Rejected, files.NotFastForward, ValueError) as e:
                return {"error": str(e)}

        return run

    def ws() -> str:
        return whoami().workspace

    @guard
    def submit_task(repo: str, prompt: str, agent: str = "claude", base: str = "HEAD"):
        """Queue a task: the agent plans first and waits for approval."""
        t = engine.submit(ws(), repo, prompt, agent, base, owner=whoami().member).public()
        return {"task_id": t["id"], "status": t["status"]}

    @guard
    def get_task(task_id: str):
        """Return one task."""
        return runs.get_run(store, whoami(), task_id).public()

    @guard
    def list_tasks():
        """List your runs, newest first."""
        return list_runs()

    @guard
    def respond(task_id: str, answer: str):
        """Answer a waiting task, for example approve."""
        t = runs.get_run(store, whoami(), task_id)
        return engine.respond(ws(), t.id, answer).public()

    @guard
    def cancel_task(task_id: str):
        """Cancel a task."""
        t = runs.get_run(store, whoami(), task_id)
        return engine.cancel(ws(), t.id).public()

    @guard
    def fetch_command(task_id: str):
        """Return the git command that fetches a finished task's branch."""
        t = runs.get_run(store, whoami(), task_id).public()
        if t["status"] != "done":
            return {"error": "task is not done"}
        res = t.get("result") or {}
        if isinstance(res, str):
            res = json.loads(res)
        fetch = res.get("fetch", "")
        if res.get("bundle"):
            url = f"{settings.public_url.rstrip('/')}/api/tasks/{task_id}/bundle"
            return {"command": fetch.replace("<bundle-url>", url), "bundle_url": url}
        return {"command": fetch}

    @guard
    def list_runs():
        """List your runs, newest first."""
        p = whoami()
        out = [t.public() for t in engine.list(p.workspace, owner=p.member)]
        runs.mark_seen(store, p.member)
        return out

    @guard
    def give_run(run: str, email: str):
        """Hand a run to another member, by their email address."""
        return runs.give(store, engine, whoami(), run, email).public()

    @guard
    def status():
        """Notices for you, such as runs given to you."""
        return {"notices": runs.notices(store, whoami().member)}

    @guard
    def team_lessons():
        """Approved lessons the team has agreed on."""
        return learning.lessons(store, ws())

    def load(ref):
        """The waiting run and its published row, or an error dict."""
        task = runs.get_run(store, whoami(), ref)
        if task.status != "waiting":
            return None, None, {"error": f"run is {task.status}, not waiting"}
        row = filestore.get(store, task.id)
        if row is None or row["state"] != "ready":
            return None, None, {"error": "files not published yet"}
        return task, row, None

    def cache_of(task):
        return repocache.cache_for(settings.data_dir, task.ws, task.repo)

    @guard
    def list_files(ref: str):
        """List the files of a waiting run (edits if any, else the gate commit)."""
        task, row, err = load(ref)
        if err:
            return err
        sha = row["edits"]["sha"] if row["edits"] else row["sha"]
        return {"files": files.list_files(cache_of(task), sha)}

    @guard
    def read_file(ref: str, path: str):
        """Read one file of a waiting run."""
        task, row, err = load(ref)
        if err:
            return err
        sha = row["edits"]["sha"] if row["edits"] else row["sha"]
        return {"path": path, "text": files.read_file(cache_of(task), sha, path)}

    @guard
    def write_file(ref: str, path: str, text: str):
        """Write one file of a waiting run as a new commit on top of the edits."""
        task, row, err = load(ref)
        if err:
            return err
        sha, changed = files.write_file(cache_of(task), task.id, row["sha"], path, text)
        filestore.set_edits(store, task.id, sha, changed)
        return {"sha": sha, "files": changed}

    fns = (submit_task, get_task, list_tasks, respond, cancel_task, fetch_command, team_lessons,
           list_runs, give_run, status, list_files, read_file, write_file)
    return {f.__name__: f for f in fns}


def build_mcp(engine, store, settings) -> FastMCP:
    mcp = _new()
    for fn in tools(engine, store, settings, auth.current_principal).values():
        mcp.tool()(fn)
    return mcp
