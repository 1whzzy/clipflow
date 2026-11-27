r"""Platform HTTP API, phase 1 (design §10). Tasks run in worker processes, never here: restarting
the API doesn't touch a running task.

    .\.venv\Scripts\python.exe -m foundation.api          # http://127.0.0.1:8100/docs

A waiting task shows its open question in `waiting_for`; answer it with
`POST /tasks/{id}/decisions {waitpoint_id, body}`. Answering an old question gives 409 with what is
open now; an answer that doesn't fit the question gives 422 and the question stays open.

Feishu sign-in protects the shared workbench when configured. The API listens on localhost.
"""
from datetime import datetime

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .artifacts import LocalStorage
from .auth import FeishuSettings, actor, mount_login, scoped_request_id
from .contracts import upload_refs
from .ops import CheckRunner, Ops
from .registry import APPS, Registry, UnknownWorkflow, check_decision
from .tasks import Conflict, InvalidDecision, NotFound, StaleDecision, TaskRepository
from .uploads import CHUNK_BYTES, Uploads, WrongOffset

PUBLIC = ("id", "workflow_type", "workflow_version", "status", "outcome", "summary", "current_step", "progress",
          "error", "error_kind", "attempt", "cancel_requested", "created_by", "created_at", "started_at",
          "updated_at", "finished_at")


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_type: str
    input: dict
    client_request_id: str | None = Field(None, max_length=128,
                                          description="send the same value again to get the same task back")
    created_by: str = Field("", max_length=128)
    priority: int = Field(0, ge=-100, le=100)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waitpoint_id: str
    body: dict
    by: str = Field("", max_length=128)
    client_request_id: str | None = Field(None, max_length=128,
                                          description="send the same value again to get the same decision back")


class UploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=500)
    bytes: int = Field(ge=0)
    mime: str | None = Field(None, max_length=200)
    created_by: str = Field("", max_length=128)


UPLOAD = ("id", "name", "bytes", "received", "mime", "sha256", "status", "created_by", "created_at", "completed_at")


class ByBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str = Field("", max_length=128)


def _plain(row: dict, keys) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if k in keys}


WAITPOINT = ("id", "key", "subject_version", "payload", "actions", "decision_schema", "view", "status",
             "close_reason", "opened_at", "closed_at", "decision_id", "decision_body", "decided_by", "decided_at",
             "rejected_reason")


def file_view(a: dict) -> dict:
    return {"id": a["id"], "kind": a["kind"], "mime": a["mime"], "bytes": a["bytes"], "sha256": a["sha256"],
            "url": f"/api/v1/artifacts/{a['id']}"}


def waitpoint_view(row: dict | None, previews: list[dict] | None = None) -> dict | None:
    if row is None:
        return None
    out = _plain(row, WAITPOINT)
    if previews is not None:
        out["previews"] = [file_view(a) for a in previews]
    return out


def task_view(row: dict, artifacts: list[dict] | None = None, waiting_for: dict | None = None,
              previews: list[dict] | None = None) -> dict:
    out = _plain(row, PUBLIC)
    if row["status"] == "waiting":
        out["waiting_for"] = waitpoint_view(waiting_for, previews)
    if artifacts is not None:
        out["artifacts"] = [file_view(a) for a in artifacts]
    return out


def create_app(repo: TaskRepository, registry: Registry, storage: LocalStorage, modules: dict | None = None,
               checks: CheckRunner | None = None, platform: dict | None = None, apps: list[dict] | None = None,
               feishu: FeishuSettings | None = None
               ) -> FastAPI:
    """`modules`: name → APIRouter, a business module's own pages' API, served at /api/v1/<name>/.
    `checks`: the health checks the 运行状态 page shows (disk space, the model gateway …)."""
    api = FastAPI(title="foundation", version="0.1",
                  description="Runs registered workflows as tasks: queue, workers, status, files.")
    mount_login(api, repo.pool, feishu)
    uploads = Uploads(repo.pool, storage)
    platform = platform or {"name": "智能工作平台", "tagline": ""}
    apps = APPS if apps is None else apps

    def title_of(row: dict) -> str:
        """The workflow's short name for this task (its optional `title(input)`), for lists."""
        try:
            fn = getattr(registry.load(row["workflow_type"]), "title", None)
            return str(fn(row["input"]))[:60] if fn else ""
        except Exception:  # noqa: BLE001  an unknown type or an odd input: no title, never an error
            return ""

    def get_task(task_id: str) -> dict:
        try:
            return repo.get(task_id)
        except NotFound:
            raise HTTPException(404, f"no task {task_id}") from None

    @api.get("/api/v1/workflows")
    def workflows():
        out = []
        for t in registry.types():
            wf = registry.load(t)
            out.append({"type": wf.type, "version": wf.version, "label": getattr(wf, "label", wf.type),
                        "input_schema": wf.input_model.model_json_schema()})
        return out

    @api.get("/api/v1/apps")
    def list_apps():
        """The platform's name and its apps (registry.APPS), each with the workflows it groups. Only
        workflows this platform has registered are listed; an app left with none is left out."""
        known, out = set(registry.types()), []
        for a in apps:
            flows = [{"type": t, "label": getattr(registry.load(t), "label", t)} for t in a["workflows"] if t in known]
            if flows:
                out.append({**a, "workflows": flows, "hidden": bool(a.get("hidden")), "pages": a.get("pages", [])})
        return {"platform": platform, "apps": out}

    @api.post("/api/v1/tasks", status_code=202)
    def submit(body: SubmitBody, request: Request):
        try:
            wf = registry.load(body.workflow_type)
            inp = wf.input_model.model_validate(body.input)
        except UnknownWorkflow as e:
            raise HTTPException(422, str(e)) from None
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        for ref in upload_refs(wf.input_model.model_json_schema(), body.input):  # files must have arrived
            try:
                uploads.path(ref)
            except NotFound:
                raise HTTPException(422, f"no upload {ref}") from None
            except Conflict as e:
                raise HTTPException(422, str(e)) from None
        try:
            row, created = repo.submit(wf.type, wf.version, inp.model_dump(mode="json"),
                                       created_by=actor(request, body.created_by),
                                       client_request_id=scoped_request_id(request, body.client_request_id),
                                       priority=body.priority)
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        return {"task_id": row["id"], "status": row["status"], "created": created}

    @api.get("/api/v1/tasks")
    def list_tasks(status: str | None = None,
                   workflow_type: str | None = Query(None, description="one type, or several separated by commas"),
                   limit: int = Query(50, ge=1, le=500)):
        return [{**task_view(r), "title": title_of(r)}
                for r in repo.list(status=status, workflow_type=workflow_type.split(",") if workflow_type else None,
                                   limit=limit)]

    @api.get("/api/v1/tasks/{task_id}")
    def get(task_id: str):
        row = get_task(task_id)
        wp = repo.open_waitpoint(task_id)
        previews = repo.previews([wp["id"]])[wp["id"]] if wp else None
        return {**task_view(row, repo.artifacts(task_id), wp, previews), "input": row["input"], "title": title_of(row)}

    @api.get("/api/v1/tasks/{task_id}/waitpoints")
    def waitpoints(task_id: str):
        get_task(task_id)
        rows = repo.waitpoints(task_id)
        previews = repo.previews([w["id"] for w in rows])
        return [waitpoint_view(w, previews[w["id"]]) for w in rows]

    @api.post("/api/v1/tasks/{task_id}/decisions", status_code=202)
    def decide(task_id: str, d: DecisionBody, request: Request):
        row = get_task(task_id)
        try:
            wf = registry.load(row["workflow_type"], row["workflow_version"])
        except LookupError as e:
            raise HTTPException(409, str(e)) from None
        try:
            decision, created = repo.decide(task_id, d.waitpoint_id, d.body, by=actor(request, d.by),
                                            client_request_id=scoped_request_id(request, d.client_request_id),
                                            check=lambda wp, body: check_decision(wf, wp, body))
        except StaleDecision as e:
            raise HTTPException(409, {"message": str(e), "status": e.status,
                                      "waiting_for": waitpoint_view(e.current)}) from None
        except InvalidDecision as e:
            raise HTTPException(422, str(e)) from None
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        return {"decision_id": decision["id"], "created": created, "status": get_task(task_id)["status"]}

    @api.get("/api/v1/tasks/{task_id}/events")
    def events(task_id: str):
        get_task(task_id)
        return [{**e, "at": e["at"].isoformat()} for e in repo.events(task_id)]

    @api.post("/api/v1/tasks/{task_id}/retry", status_code=202)
    def retry(task_id: str, request: Request, body: ByBody | None = None):
        get_task(task_id)
        try:
            return task_view(repo.retry(task_id, by=actor(request, body.by if body else "")))
        except Conflict as e:
            raise HTTPException(409, str(e)) from None

    @api.post("/api/v1/tasks/{task_id}/cancel", status_code=202)
    def cancel(task_id: str, request: Request, body: ByBody | None = None):
        get_task(task_id)
        try:
            return task_view(repo.request_cancel(task_id, by=actor(request, body.by if body else "")))
        except Conflict as e:
            raise HTTPException(409, str(e)) from None

    @api.get("/api/v1/artifacts/{artifact_id}")
    def download(artifact_id: str):
        try:
            a = repo.artifact(artifact_id)
        except NotFound:
            raise HTTPException(404, f"no artifact {artifact_id}") from None
        path = storage.path(a["location"])
        if not path.is_file():
            raise HTTPException(410, f"artifact {artifact_id} is recorded but its file is missing")
        return FileResponse(path, media_type=a["mime"], filename=path.name)

    @api.get("/api/v1/health")
    def health():
        """Queues, live workers and warnings (ops.py); 503 when the database can't be reached."""
        try:
            return Ops(repo.pool).health(registry.types(), checks.results if checks else [],
                                         checks.checked_at if checks else None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"database": "down", "error": f"{type(e).__name__}: {e}"[:500],
                                 "warnings": [{"kind": "database", "message": "数据库连不上：任务和审核都暂停了"}]},
                                status_code=503)

    # ---- uploads: resumable, in chunks (uploads.py) ----

    @api.post("/api/v1/uploads", status_code=201)
    def start_upload(body: UploadBody, request: Request):
        try:
            row = uploads.create(body.name, body.bytes, body.mime, actor(request, body.created_by))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return {**_plain(row, UPLOAD), "chunk_bytes": CHUNK_BYTES}

    @api.get("/api/v1/uploads/{upload_id}")
    def get_upload(upload_id: str):
        try:
            return _plain(uploads.get(upload_id), UPLOAD)
        except NotFound:
            raise HTTPException(404, f"no upload {upload_id}") from None

    @api.put("/api/v1/uploads/{upload_id}")
    async def put_chunk(upload_id: str, request: Request, offset: int = Query(ge=0)):
        data = await request.body()
        try:
            row = await run_in_threadpool(uploads.append, upload_id, offset, data)
        except NotFound:
            raise HTTPException(404, f"no upload {upload_id}") from None
        except WrongOffset as e:  # resume from here
            raise HTTPException(409, {"message": str(e), "received": e.received}) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return _plain(row, UPLOAD)

    for name, router in (modules or {}).items():  # e.g. Clipflow's material library (phase 4)
        api.include_router(router, prefix=f"/api/v1/{name}")

    # the workbench (phase 3): plain files, served last so /api/v1 always wins
    api.mount("/", StaticFiles(directory=Path(__file__).with_name("web"), html=True), name="workbench")
    return api


def main() -> None:  # pragma: no cover
    import uvicorn

    from .db import check, open_pool
    from .dispatch.postgres import PostgresDispatcher
    from .settings import PlatformSettings, read_env

    s = PlatformSettings.load()
    feishu = FeishuSettings.from_env(read_env())
    pool = open_pool(s.database_url, s.schema)
    check(pool)
    repo = TaskRepository(pool, PostgresDispatcher(s.database_url, s.schema))
    from .ops import CheckRunner
    from .registry import load_checks, load_modules

    checks = CheckRunner(load_checks(), storage_root=s.files_dir).start()
    uvicorn.run(create_app(repo, Registry(), LocalStorage(s.files_dir), load_modules(), checks,
                           {"name": s.brand, "tagline": s.tagline}, feishu=feishu),
                host="127.0.0.1", port=s.api_port)


if __name__ == "__main__":
    main()
