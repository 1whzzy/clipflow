r"""HTTP API: how a front end drives a run and its two human pauses (spec appendix D, /api/v1/runs).

    POST /api/v1/runs                     submit a job (a RunRequest)         → 202 {run_id}
    GET  /api/v1/runs/{run_id}            where it is: running / waiting (with the pause) / done …
    POST /api/v1/runs/{run_id}/resume     answer the pause it is waiting at   → 202
    POST /api/v1/runs/{run_id}/continue   after a technical failure, run on from the failed step

A run executes in a background thread; the client polls GET. Each run is one LangGraph thread
(thread_id = run_id), so with the Postgres checkpointer any API process can answer any run.
An invalid answer (stale version, approving while a must-fix problem is open, …) is not an HTTP
error: the run pauses again, and GET shows the reason in `waiting_for.error`.

    .\.venv\Scripts\python.exe -m clipflow.api          # http://127.0.0.1:8000/docs
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from collections.abc import Callable

from fastapi import FastAPI, HTTPException
from langgraph.types import Command
from pydantic import ValidationError

from .contracts import Delivery, RunRequest
from .graph.context import RunContext


class RunService:
    """Runs workflow threads in the background and reports where each one is."""

    def __init__(self, graph, ctx: RunContext, submit: Callable[[Callable[[], None]], Any] | None = None):
        self.graph, self.ctx = graph, ctx
        self._submit = submit or ThreadPoolExecutor(max_workers=4, thread_name_prefix="run").submit
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._errors: dict[str, str] = {}

    @staticmethod
    def cfg(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 80}

    def _launch(self, run_id: str, inp) -> None:
        with self._lock:
            if run_id in self._running:
                raise HTTPException(409, f"run {run_id} is already running")
            self._running.add(run_id)
            self._errors.pop(run_id, None)

        def work():
            try:
                self.graph.invoke(inp, self.cfg(run_id), context=self.ctx)
            except Exception as e:  # noqa: BLE001  kept for GET; the checkpoint stays at the failed step
                self._errors[run_id] = f"{type(e).__name__}: {e}"
            finally:
                with self._lock:
                    self._running.discard(run_id)

        self._submit(work)

    def start(self, request: RunRequest) -> str:
        if self.graph.get_state(self.cfg(request.run_id)).values:
            raise HTTPException(409, f"run {request.run_id} already exists")
        self._launch(request.run_id, {"input": request.model_dump(mode="json")})
        return request.run_id

    def resume(self, run_id: str, answer: dict) -> None:
        if not self._waiting(run_id):
            raise HTTPException(409, f"run {run_id} is not waiting for an answer")
        self._launch(run_id, Command(resume=answer))

    def continue_failed(self, run_id: str) -> None:
        snap = self._snapshot(run_id)
        if not snap.next or self._waiting(run_id) or run_id in self._running:
            raise HTTPException(409, f"run {run_id} has nothing to continue")
        self._launch(run_id, None)  # None = run on from the last checkpoint (not Command(resume=None))

    def status(self, run_id: str) -> dict:
        if run_id in self._running and not self.graph.get_state(self.cfg(run_id)).values:
            # accepted, but the background thread hasn't saved its first checkpoint yet
            return {"run_id": run_id, "status": "running", "waiting_for": None, "next": [], "flags": [], "error": None}
        snap = self._snapshot(run_id)
        values = snap.values
        waiting = self._waiting(run_id)
        if run_id in self._running:
            status = "running"
        elif waiting:
            status = "waiting"
        elif run_id in self._errors:
            status = "failed"  # a step raised; POST …/continue after fixing the cause
        else:
            status = values.get("status", "unknown")
        out = {"run_id": run_id, "status": status, "waiting_for": waiting[0] if waiting else None,
               "next": list(snap.next), "flags": values.get("flags", []), "error": self._errors.get(run_id)}
        if values.get("delivery"):
            out["video"] = self.ctx.store.get(values["delivery"], Delivery).video_path
        return out

    def _snapshot(self, run_id: str):
        snap = self.graph.get_state(self.cfg(run_id))
        if not snap.values:
            raise HTTPException(404, f"no run {run_id}")
        return snap

    def _waiting(self, run_id: str) -> list[dict]:
        if run_id in self._running:
            return []
        snap = self._snapshot(run_id)
        return [i.value for task in snap.tasks for i in task.interrupts]


def create_app(service: RunService) -> FastAPI:
    api = FastAPI(title="clipflow", version="0.1",
                  description="Product + footage → finished selling video, with two human checkpoints.")

    @api.post("/api/v1/runs", status_code=202)
    def submit(body: dict):
        try:
            request = RunRequest.model_validate(body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from None
        return {"run_id": service.start(request), "status": "queued"}

    @api.get("/api/v1/runs/{run_id}")
    def get(run_id: str):
        return service.status(run_id)

    @api.post("/api/v1/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str, answer: dict):
        service.resume(run_id, answer)
        return {"run_id": run_id, "status": "running"}

    @api.post("/api/v1/runs/{run_id}/continue", status_code=202)
    def cont(run_id: str):
        service.continue_failed(run_id)
        return {"run_id": run_id, "status": "running"}

    return api


def main() -> None:  # pragma: no cover - needs the real workers
    import uvicorn

    from .graph.build import compile_app
    from .run import real_context, reachable_db
    from .settings import Settings
    from .storage import open_checkpointer

    settings = Settings.load()
    with open_checkpointer(reachable_db(settings)) as saver:
        service = RunService(compile_app(saver), real_context(settings))
        uvicorn.run(create_app(service), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
