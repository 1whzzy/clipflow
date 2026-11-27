"""Which workflows exist (design §3). Registered in code, by type; each task records the version it was
created with, and a worker refuses a task whose version it doesn't have, rather than running old
saved state on new code.

Adding a workflow = writing its class under `workflows/` and adding one line to `WORKFLOWS`.
"""
from importlib import import_module
from pathlib import Path
from typing import ClassVar, Protocol

from pydantic import BaseModel

from .contracts import Command, Finished, TaskView, Waiting, WaitpointView


class StepContextLike(Protocol):
    task_id: str

    def check(self) -> None: ...
    def step(self, name: str, progress: float | None = None) -> None: ...
    def log(self, kind: str, data: dict) -> None: ...
    def upload_path(self, upload_id: str) -> Path: ...


class Workflow(Protocol):
    type: ClassVar[str]
    version: ClassVar[int]  # bump when stored inputs or saved state are no longer compatible
    input_model: ClassVar[type[BaseModel]]  # checked at submit, before the task exists
    timeout_s: ClassVar[float]  # per advance() call
    # optional: `label: ClassVar[str]`, the name people see in the workbench (the type otherwise)
    # optional: `title(input: dict) -> str`, a short name for one task in lists (e.g. the product)

    def advance(self, task: TaskView, command: Command, ctx: StepContextLike) -> Finished | Waiting:
        """Run until the workflow finishes or needs a person. Call `ctx.step()` / `ctx.check()` between
        steps. Raise to fail the task; the platform records the error. On `resume`, `task.resume`
        holds the question and the answer.

        `command` may be re-run after a crash (a retry repeats the last command's work from the
        workflow's own save point), so a workflow must tolerate seeing the same command twice."""

    def check_decision(self, waitpoint: WaitpointView, body: dict) -> str | None:
        """A quick check of an answer before it is accepted (shape, offered actions). Return the
        problem, or None. Runs inside the decision's transaction: no I/O, no model calls."""


# type → "module:Class". Imported only when first used, so the API doesn't load every engine.
WORKFLOWS: dict[str, str] = {
    "echo": "workflows.echo:EchoWorkflow",  # a trivial workflow for tests and smoke checks
    "clip": "workflows.clip:ClipWorkflow",  # Clipflow: product + footage → finished selling video
    "report": "workflows.report:ReportWorkflow",  # topic + notes → reviewed document (the reuse proof, §6)
    "ingest": "workflows.ingest:IngestWorkflow",  # a replay or product footage → the material library (phase 4)
}

# A business module's own API (its pages need more than tasks): name → "module:function" returning a
# FastAPI APIRouter, served at /api/v1/<name>/. Loaded only by the API process.
MODULES: dict[str, str] = {
    "library": "clipflow.library.api:router",  # the material library: recall by product, play a segment
}


# A business module's health checks, shown on 运行状态: name → "module:function" returning a list of
# {name, ok, message, ms?}. Run in the background every few minutes by the API process (ops.py).
CHECKS: dict[str, str] = {
    "clipflow": "clipflow.health:checks",  # the model gateway, the GPU
}


# What people see on the platform's home page: apps. An app groups the workflows (and a module's pages)
# that serve one business job; the video-editing workflow is one app among others. A workflow that
# isn't registered here still runs and shows in the task list. `hidden` apps are left off the home
# page (tools for whoever runs the platform). Order = order on the home page.
APPS: list[dict] = [
    {"id": "video", "name": "带货视频剪辑", "icon": "video",
     "description": "从直播回放或商品实拍自动剪出带货短视频；分镜和成片由剪辑审核后交付。",
     "workflows": ["clip", "ingest"],
     "pages": [{"label": "素材库", "route": "library", "icon": "library",
                "description": "按商品召回处理过的直播讲解段和实拍镜头"}]},
    {"id": "docs", "name": "文档起草", "icon": "doc",
     "description": "按主题和要点起草文档；你批准、直接修改，或写意见让它重写。",
     "workflows": ["report"]},
    {"id": "selftest", "name": "平台自测", "icon": "spark", "hidden": True,
     "description": "检查任务、审核和执行进程是否正常的小任务。",
     "workflows": ["echo"]},
]


def load_checks(entries: dict[str, str] | None = None) -> dict:
    """name → callable. One that can't be imported becomes a check that reports why."""
    out = {}
    for name, target in (CHECKS if entries is None else entries).items():
        module, _, attr = target.partition(":")
        try:
            out[name] = getattr(import_module(module), attr)
        except Exception as e:  # noqa: BLE001
            out[name] = lambda e=e, name=name: [{"name": name, "ok": False, "message": f"检查程序加载失败：{e}"}]
    return out


def load_modules(entries: dict[str, str] | None = None) -> dict:
    """name → APIRouter. A module that can't start (e.g. its tables are missing) is left out with a
    warning instead of taking the whole API down."""
    import logging

    out = {}
    for name, target in (MODULES if entries is None else entries).items():
        module, _, attr = target.partition(":")
        try:
            out[name] = getattr(import_module(module), attr)()
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning("module %s not loaded: %s", name, e)
    return out


def check_decision(wf: Workflow, waitpoint: WaitpointView, body: dict) -> str | None:
    """The workflow's quick check if it has one; otherwise only that the action is offered."""
    if hasattr(wf, "check_decision"):
        return wf.check_decision(waitpoint, body)
    verdict = body.get("verdict") or body.get("action")
    if waitpoint.actions and verdict not in waitpoint.actions:
        return f"choose one of {waitpoint.actions}"
    return None


class UnknownWorkflow(LookupError):
    pass


class VersionMismatch(LookupError):
    pass


class Registry:
    def __init__(self, entries: dict[str, str | type] | None = None):
        self._entries = dict(WORKFLOWS if entries is None else entries)
        self._loaded: dict[str, Workflow] = {}

    def types(self) -> list[str]:
        return sorted(self._entries)

    def load(self, workflow_type: str, version: int | None = None) -> Workflow:
        if workflow_type not in self._entries:
            raise UnknownWorkflow(f"unknown workflow type {workflow_type!r}; registered: {self.types()}")
        if workflow_type not in self._loaded:
            target = self._entries[workflow_type]
            if isinstance(target, str):
                module, _, attr = target.partition(":")
                target = getattr(import_module(module), attr)
            wf = target()
            if wf.type != workflow_type:
                raise TypeError(f"{target} declares type {wf.type!r}, registered as {workflow_type!r}")
            self._loaded[workflow_type] = wf
        wf = self._loaded[workflow_type]
        if version is not None and version != wf.version:
            raise VersionMismatch(f"task needs {workflow_type} version {version}; this worker has version {wf.version}")
        return wf
