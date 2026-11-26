"""REST routes for run files: publish, list, bundle download, edit upload."""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import files, filestore, repocache, runs
from .errors import BadTransition, NotFound


def _err(code: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def files_body(store, task, cache) -> dict:
    row = filestore.get(store, task.id)
    if row is None or row["state"] == "gated":
        raise BadTransition("run was never published")
    if row["state"] == "pending":
        return {"state": "pending"}
    at = row["edits"]["sha"] if row["edits"] else row["sha"]
    return {"state": "ready", "branch": row["branch"], "sha": row["sha"], "pushed": row["pushed"],
            "via": "remote" if row["pushed"] else "slcloud", "files": files.list_files(cache, at),
            "plans": row["plans"], "edits": row["edits"]}


def make_files_router(store, engine, whoami, settings) -> APIRouter:
    router = APIRouter()
    bundle_dir = os.path.join(settings.data_dir, "bundles")

    def cache_of(task):
        return repocache.cache_for(settings.data_dir, task.ws, task.repo)

    def resolve(ref):
        task = runs.get_run(store, whoami(), ref)
        if task.status != "waiting":
            raise BadTransition(f"run is {task.status}, not waiting")
        return task

    def ready_row(task):
        row = filestore.get(store, task.id)
        if row is None or row["state"] == "gated":
            raise BadTransition("run was never published")
        if row["state"] != "ready":
            raise BadTransition("files are not ready yet")
        return row

    def guard(fn):
        try:
            return fn()
        except NotFound:
            return _err(404, "not found")
        except (BadTransition, files.NotFastForward) as e:
            return _err(409, str(e))
        except ValueError as e:
            return _err(400, str(e))

    @router.post("/api/tasks/{ref:path}/publish")
    def publish(ref: str):
        def go():
            task = resolve(ref)
            if filestore.request_publish(store, task.id) == "pending":
                return JSONResponse({"state": "pending"}, status_code=202)
            return files_body(store, task, cache_of(task))
        return guard(go)

    @router.get("/api/tasks/{ref:path}/files/bundle")
    def bundle(ref: str):
        def go():
            task = resolve(ref)
            row = ready_row(task)
            os.makedirs(bundle_dir, exist_ok=True)
            out = files.make_bundle(cache_of(task), task.id,
                                    os.path.join(bundle_dir, f"{task.id}.{row['sha'][:8]}.bundle"))
            return FileResponse(out, media_type="application/octet-stream")
        return guard(go)

    @router.get("/api/tasks/{ref:path}/files")
    def list_(ref: str):
        def go():
            task = resolve(ref)
            return files_body(store, task, cache_of(task))
        return guard(go)

    @router.post("/api/tasks/{ref:path}/files")
    async def upload(ref: str, request: Request):
        is_json = request.headers.get("content-type", "").startswith("application/json")
        body = await request.body()
        jbody = None
        if is_json:
            try:
                jbody = await request.json()
            except ValueError:
                jbody = None

        def go():
            task = resolve(ref)
            row = ready_row(task)
            cache = cache_of(task)
            precheck = lambda: filestore.require_waiting(store, task.id)  # noqa: E731
            record = lambda sha, names: filestore.set_edits(store, task.id, sha, names)  # noqa: E731
            if is_json:
                sha = jbody.get("sha") if isinstance(jbody, dict) else None
                if not isinstance(sha, str) or not sha:
                    raise ValueError("sha required")
                new, changed = files.accept_remote(cache, task.id, row["sha"], sha, precheck, record)
            else:
                base = request.query_params.get("base")
                if base is not None and base != row["sha"]:
                    raise BadTransition("base is not the gate commit")
                if not body:
                    raise ValueError("empty bundle")
                os.makedirs(bundle_dir, exist_ok=True)
                fd, tmp = tempfile.mkstemp(suffix=".bundle", dir=bundle_dir)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(body)
                    new, changed = files.accept_bundle(cache, task.id, row["sha"], tmp,
                                                    precheck, record)
                finally:
                    os.unlink(tmp)
            return files_body(store, task, cache)
        return await run_in_threadpool(guard, go)

    return router
