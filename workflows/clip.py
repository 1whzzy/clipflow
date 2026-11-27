"""Clipflow on the platform (docs/PLATFORM_DESIGN.md §5): the adapter between platform tasks and the
Clipflow LangGraph engine. Clipflow itself is unchanged.

- Ids: the task id is Clipflow's `run_id` and its LangGraph `thread_id`.
- State: Clipflow keeps its own progress in LangGraph checkpoints in Postgres, so any worker process
  can carry a task on. The platform never reads that state; it only sees what `advance` returns.
- Pauses → platform questions, mapped one by one (they are not alike):

  | pause           | key          | version          | actions                | a bad answer                     |
  |-----------------|--------------|------------------|------------------------|----------------------------------|
  | product missing | needs_input  | none → 0         | provide_range, cancel  | Clipflow doesn't ask again, so   |
  |                 |              |                  | (added here)           | it is caught here first (422)    |
  | plan            | confirm_plan | plan version     | from the pause         | Clipflow asks again, with reason |
  | video           | review_video | timeline version | from the pause         | same                             |

- Endings: `done` delivers the MP4 (required), cover and caption file when Clipflow made them, and
  the approved plan as JSON. `rejected` / `stopped` / `cancelled` are normal endings without files.
  A run Clipflow itself ended as `failed` (QC that kept failing) fails the task, with the reason.
"""
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, create_model, model_validator

from clipflow.contracts import Decision, Delivery, Plan, QCReport, Review, RunRequest
from clipflow.contracts.request import parse_ms
from foundation.contracts import ArtifactOut, Finished, Waiting

REVIEWS = {"confirm_plan": "plan", "review_video": "video"}  # pause kind → Decision.stage
ENDINGS = {"rejected": "rejected_by_reviewer", "stopped": "stopped", "cancelled": "cancelled_by_reviewer"}
FILLED_BY_PLATFORM = ("stage", "by", "at", "schema_version")


def _without(model: type[BaseModel], name: str, drop: tuple[str, ...]) -> type[BaseModel]:
    fields = {n: (f.annotation, f) for n, f in model.model_fields.items() if n not in drop}
    return create_model(name, __config__=ConfigDict(extra="forbid", frozen=True), **fields)


class ClipInput(_without(RunRequest, "_ClipInputFields", ("run_id",))):
    """A clip job: a RunRequest without `run_id` (the platform's task id is used)."""

    @model_validator(mode="after")
    def _is_a_valid_request(self):
        try:
            RunRequest.model_validate({"run_id": "check", **self.model_dump(mode="json")})
        except ValidationError as e:
            raise ValueError("; ".join(x["msg"] for x in e.errors())) from None
        return self


ReviewAnswer = _without(Decision, "ReviewAnswer", FILLED_BY_PLATFORM)  # what a reviewer sends


class RangeAnswer(BaseModel):
    """The answer to "product not found": a rough time range in the replay, or cancel."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["cancel"] | None = None
    start: str | int | None = None  # 1:02:30, 3:56, 236.5s or milliseconds
    end: str | int | None = None

    @model_validator(mode="after")
    def _range_or_cancel(self):
        if self.action == "cancel":
            if self.start is not None or self.end is not None:
                raise ValueError("cancel takes no time range")
            return self
        if self.start is None or self.end is None:
            raise ValueError("give both start and end (e.g. 1:02:30), or action=cancel")
        start, end = parse_ms(self.start), parse_ms(self.end)
        if end <= start:
            raise ValueError("end must be after start")
        return self


class ClipRunFailed(RuntimeError):
    """Clipflow ended the run as failed (e.g. QC kept failing after its automatic retries)."""


def _errors(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, x['loc'])) or 'body'}: {x['msg']}" for x in e.errors())


class ClipWorkflow:
    type = "clip"
    label = "带货视频"
    version = 1
    input_model = ClipInput
    timeout_s = 3600.0  # one advance() runs to the next pause: a cold plan stage takes ~6 min

    @staticmethod
    def title(inp: dict) -> str:
        return f"{inp.get('product_id', '')} · {'素材带货' if inp.get('mode') == 'material' else '切片'}"

    def __init__(self, app=None, engine_ctx=None, root: Path | None = None):
        """`app` and `engine_ctx` are built on first use from .env (Postgres checkpoints, the real
        workers), so the API can load this class for its input model without starting any engine."""
        self._app, self._ctx, self._stack = app, engine_ctx, None
        if root is None:
            from clipflow.settings import ROOT as root
        self.root = Path(root)

    def _engine(self):
        if self._app is None:
            from clipflow.graph.build import compile_app
            from clipflow.run import real_context
            from clipflow.settings import Settings
            from clipflow.storage import open_checkpointer

            settings = Settings.load()
            if not settings.database_url:
                raise RuntimeError("Clipflow on the platform needs DATABASE_URL: its progress must be saved in "
                                   "Postgres so another worker process can carry a task on")
            self._stack = ExitStack()
            self._app = compile_app(self._stack.enter_context(open_checkpointer(settings.database_url)))
            self._ctx = real_context(settings)
        return self._app, self._ctx

    # ---- run ------------------------------------------------------------------------------------

    def advance(self, task, command, ctx) -> Finished | Waiting:
        app, engine_ctx = self._engine()
        cfg = {"configurable": {"thread_id": task.engine_ref.get("thread_id", task.id)}, "recursion_limit": 80}
        snap = app.get_state(cfg)
        pending = [i for t in snap.tasks for i in t.interrupts]
        if not snap.values:  # a start, or a retry of a start that failed before its first checkpoint
            inp = {"input": RunRequest.model_validate({"run_id": task.id, **task.input}).model_dump(mode="json")}
        elif command.kind == "resume" and any(i.id == task.resume.waitpoint.engine_key for i in pending):
            inp = self._resume_command(task)
        else:
            # continue after a failure, or a command repeated after a crash that Clipflow already
            # applied (e.g. the answer was consumed before the worker died): carry on from the last
            # checkpoint instead of starting again or answering twice
            inp = None
            if command.kind != "continue":
                ctx.log("command_already_applied", {"command": command.kind})
        if inp is not None or (snap.next and not pending):
            for _ns, update in app.stream(inp, cfg, context=engine_ctx, stream_mode="updates", subgraphs=True):
                ctx.check()
                for node in update:
                    if node != "__interrupt__":
                        ctx.step(node)
        return self._outcome(app.get_state(cfg), engine_ctx, ctx)

    def _resume_command(self, task):
        from langgraph.types import Command

        wp, d = task.resume.waitpoint, task.resume.decision
        body = dict(d.body)
        if wp.key in REVIEWS:  # Clipflow records who decided and when
            body |= {"by": d.by, "at": d.at.isoformat()}
        return Command(resume={wp.engine_key: body})

    def _outcome(self, snap, engine_ctx, ctx) -> Finished | Waiting:
        pending = [i for t in snap.tasks for i in t.interrupts]
        if len(pending) > 1:
            raise RuntimeError(f"Clipflow is waiting at {len(pending)} pauses at once; expected one")
        values = snap.values
        if pending:
            return self._waiting(pending[0], values, engine_ctx)
        status, flags = values.get("status"), values.get("flags", [])
        if status == "done":
            return self._delivered(values, engine_ctx, ctx)
        if status in ENDINGS:
            return Finished(outcome=ENDINGS[status], summary={"flags": flags[-50:]})
        if status == "failed":
            reasons = [f for f in flags if f.startswith(("qc_failed", "failed"))] or flags[-5:]
            raise ClipRunFailed("Clipflow ended the run as failed: " + "; ".join(reasons))
        raise RuntimeError(f"Clipflow stopped without a pause or an ending (status {status!r}, next {snap.next})")

    def _waiting(self, intr, values: dict, engine_ctx) -> Waiting:
        p = dict(intr.value)
        kind = p.get("kind")
        previews = []
        # Clipflow's pause refers to some things by their id in its own store; the review page can't
        # read that store, so the adapter puts the contents in the question instead.
        if kind == "confirm_plan" and isinstance(p.get("review"), str):
            p["review"] = engine_ctx.store.get(p["review"], Review).model_dump(mode="json")
        if kind == "review_video":
            if isinstance(p.get("qc"), str):
                p["qc"] = engine_ctx.store.get(p["qc"], QCReport).model_dump(mode="json")
            if values.get("plan"):  # the shots, so the reviewer can name the one to change
                p["plan"] = engine_ctx.store.get(values["plan"], Plan).model_dump(mode="json")
            previews = [ArtifactOut(kind="preview_video", path=self._path(p["video"]), mime="video/mp4")]
        p = json.loads(json.dumps(p, ensure_ascii=False, default=str))  # plain JSON for the database
        if kind == "needs_input":
            return Waiting(key=kind, subject_version=0, payload=p, actions=["provide_range", "cancel"],
                           decision_schema=RangeAnswer.model_json_schema(), view="clip.needs_input",
                           engine_key=intr.id)
        if kind in REVIEWS:
            return Waiting(key=kind, subject_version=p["version"], payload=p, actions=p["actions"],
                           decision_schema=ReviewAnswer.model_json_schema(), view=f"clip.{kind}",
                           engine_key=intr.id, error=p.get("error"), previews=previews)
        raise RuntimeError(f"unknown Clipflow pause {kind!r}")

    def _delivered(self, values, engine_ctx, ctx) -> Finished:
        delivery = engine_ctx.store.get(values["delivery"], Delivery)
        plan = engine_ctx.store.get(values["plan"], Plan)
        plan_file = ctx.artifact_dir / f"plan_v{plan.version}.json"
        tmp = plan_file.with_suffix(".part")
        tmp.write_text(plan.model_dump_json(indent=1), encoding="utf-8")
        tmp.replace(plan_file)
        files = [ArtifactOut(kind="video", path=self._path(delivery.video_path), mime="video/mp4"),
                 ArtifactOut(kind="plan", path=plan_file, mime="application/json")]
        if delivery.cover_path:
            files.append(ArtifactOut(kind="cover", path=self._path(delivery.cover_path), required=False))
        if delivery.caption_file:
            files.append(ArtifactOut(kind="captions", path=self._path(delivery.caption_file), required=False))
        return Finished(outcome="delivered", artifacts=files, summary={
            "run_id": delivery.run_id, "plan_version": delivery.plan_version,
            "timeline_version": delivery.timeline_version, "qc_passed": delivery.qc_passed,
            "waived": delivery.waived, "flags": values.get("flags", [])[-50:]})

    def _path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.root / path

    # ---- answers --------------------------------------------------------------------------------

    def check_decision(self, waitpoint, body: dict) -> str | None:
        """Shape and offered actions only. Business checks (stale version, must-fix items open) stay in
        Clipflow, which asks again with the reason."""
        if waitpoint.key == "needs_input":
            try:
                RangeAnswer.model_validate(body)
            except ValidationError as e:
                return _errors(e)
            return None
        if waitpoint.key not in REVIEWS:
            return f"unknown question {waitpoint.key!r}"
        if filled := [k for k in FILLED_BY_PLATFORM if k in body]:
            return f"{filled} are filled in by the platform; leave them out"
        try:
            d = Decision.model_validate({"stage": REVIEWS[waitpoint.key], **body})
        except ValidationError as e:
            return _errors(e)
        if d.verdict not in waitpoint.actions:
            if d.verdict == "approve" and waitpoint.payload.get("must_fix"):
                return "以下问题必须先修改，不能直接批准：" + "；".join(waitpoint.payload["must_fix"])
            return f"choose one of {waitpoint.actions}"
        return None
