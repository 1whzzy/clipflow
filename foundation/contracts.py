"""What passes between the platform and a workflow."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["pending", "running", "waiting", "succeeded", "failed", "cancelled"]
ErrorKind = Literal["step_error", "timeout", "worker_lost", "bad_version", "bad_output"]
TERMINAL = ("succeeded", "failed", "cancelled")


# A field of a workflow's input that names an uploaded file (uploads.py). The workbench draws a file
# picker for it; the platform refuses the task until the upload is complete; the workflow opens it
# with `ctx.upload_path(id)`.
UploadRef = Annotated[str, Field(pattern=r"^up_[0-9a-f]{20}$", json_schema_extra={"x-upload": True})]


def upload_refs(schema: dict, data: dict) -> list[str]:
    """The upload ids in a workflow input (top-level fields, single or lists), found by the schema mark."""
    def marked(p: dict) -> bool:
        return bool(p.get("x-upload") or any(marked(a) for a in p.get("anyOf", []))
                    or (p.get("type") == "array" and marked(p.get("items", {}))))

    out = []
    for name, prop in schema.get("properties", {}).items():
        value = data.get(name)
        if value and marked(prop):
            out += value if isinstance(value, list) else [value]
    return out


class Command(BaseModel):
    """What a pending task asks the next worker to do. Stored on the task row, so any worker in any
    process can carry it out."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["start", "continue", "resume"]
    decision_id: str | None = None  # resume only


@dataclass(frozen=True)
class WaitpointView:
    """An open (or, on resume, just answered) question to a person."""

    id: str
    key: str
    subject_version: int
    payload: dict
    actions: list[str]
    engine_key: str

    @classmethod
    def from_row(cls, row: dict) -> WaitpointView:
        return cls(id=row["id"], key=row["key"], subject_version=row["subject_version"], payload=row["payload"],
                   actions=row["actions"], engine_key=row["engine_key"])


@dataclass(frozen=True)
class DecisionView:
    id: str
    body: dict
    by: str
    at: datetime


@dataclass(frozen=True)
class Resume:
    """What a `resume` command carries to the workflow: the question and the person's answer."""

    waitpoint: WaitpointView
    decision: DecisionView


@dataclass(frozen=True)
class TaskView:
    """The task as a workflow sees it."""

    id: str
    workflow_type: str
    workflow_version: int
    input: dict
    engine_ref: dict
    command: Command
    attempt: int  # 1 on the first claim; +1 on every later claim (retry, resume, recovery)
    created_by: str
    resume: Resume | None = None  # set when command.kind == "resume"

    @classmethod
    def from_row(cls, row: dict) -> TaskView:
        return cls(id=row["id"], workflow_type=row["workflow_type"], workflow_version=row["workflow_version"],
                   input=row["input"], engine_ref=row["engine_ref"], command=Command.model_validate(row["command"]),
                   attempt=row["attempt"], created_by=row["created_by"])


class ArtifactOut(BaseModel):
    """A file the workflow delivers. Required files that are missing fail the task (`bad_output`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: str  # video, cover, captions, plan, report, text …
    path: Path
    required: bool = True
    mime: str | None = None  # guessed from the extension when empty


class Finished(BaseModel):
    """The workflow ended. `outcome` is the business result: a reviewer rejecting a plan is a normal
    ending (`rejected_by_reviewer`), not a failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: str
    artifacts: list[ArtifactOut] = []
    summary: dict = {}


class Waiting(BaseModel):
    """The workflow needs a person. The worker is released; a valid decision makes the task pending
    again with `resume`, and any worker carries on.

    Set `error` when asking again because the previous answer was not accepted (a stale version,
    approving while must-fix items are open …): the platform then marks the previous question
    `superseded`, records the reason on that decision, and the reviewer sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str
    subject_version: int = 0
    payload: dict = {}
    actions: list[str] = []
    decision_schema: dict | None = None  # JSON Schema of the answer, for the review page
    view: str = "generic"
    engine_key: str = ""  # the workflow's own id for this pause (Clipflow: the LangGraph interrupt id)
    error: str | None = None
    previews: list[ArtifactOut] = []  # files the reviewer looks at (e.g. the rendered video); not deliverables


class Stop(Exception):  # noqa: N818  a signal, not an error
    """Raised by `StepContext.check()`: the task must stop now (cancelled, timed out, lease lost)."""

    def __init__(self, reason: Literal["cancelled", "timeout", "lease_lost"]):
        super().__init__(reason)
        self.reason = reason


class BadOutput(Exception):
    """A workflow said it finished but a required file is missing."""
