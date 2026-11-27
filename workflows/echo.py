"""A deliberately trivial workflow: a few timed steps, an optional review, then the text as a file.

Used to accept the platform on its own, before any real engine is plugged in, and by the tests
(`step_s` makes a step long enough to kill or cancel; `fail_on_attempt` makes a step raise).

With `review: true` it pauses for a person the same way Clipflow's plan confirmation does: a
versioned subject, approve / edit / reject, and a stale version is refused by the workflow itself
(it asks again with the reason), not by the platform. Its only saved state is the question it asked,
so a retry after a failure starts the review over; Clipflow keeps its state in LangGraph checkpoints.
"""
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from foundation.contracts import ArtifactOut, Finished, Waiting


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10_000)
    steps: int = Field(3, ge=1, le=100)
    step_s: float = Field(0.0, ge=0, le=120)
    fail_on_attempt: list[int] = []  # raise in the last step on these attempts (1 = first run)
    review: bool = False  # ask a person to approve, edit or reject the text before delivering it


class EchoDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["approve", "edit", "reject"]
    version: int  # the draft version the reviewer looked at
    text: str | None = Field(None, min_length=1, max_length=10_000)  # the new text, for edit
    reason: str = ""

    @model_validator(mode="after")
    def _text_only_for_edit(self):
        if (self.verdict == "edit") != (self.text is not None):
            raise ValueError("text is required for edit and only allowed with edit")
        return self


class EchoWorkflow:
    type = "echo"
    label = "测试任务"
    version = 1
    input_model = EchoInput
    timeout_s = 600.0

    @staticmethod
    def title(inp: dict) -> str:
        return str(inp.get("text", ""))[:30]

    def advance(self, task, command, ctx) -> Finished | Waiting:
        inp = EchoInput.model_validate(task.input)
        if command.kind == "resume":
            return self._resume(task, ctx)
        for i in range(1, inp.steps + 1):
            ctx.step(f"step {i}/{inp.steps}", progress=round(i / inp.steps, 3))
            end = time.monotonic() + inp.step_s
            while time.monotonic() < end:  # a long step that still stops promptly when asked
                ctx.check()
                time.sleep(min(0.05, max(0.0, end - time.monotonic())))
            if i == inp.steps and task.attempt in inp.fail_on_attempt:
                raise RuntimeError(f"failing on attempt {task.attempt}, as asked")
        if inp.review:
            return self._ask(inp.text, 1)
        return self._deliver(task, command, ctx, inp.text)

    def check_decision(self, waitpoint, body: dict) -> str | None:
        """Shape only. Whether the version is current is the workflow's business, decided on resume."""
        try:
            d = EchoDecision.model_validate(body)
        except ValidationError as e:
            return "; ".join(f"{'.'.join(map(str, x['loc'])) or 'body'}: {x['msg']}" for x in e.errors())
        if d.verdict not in waitpoint.actions:
            return f"choose one of {waitpoint.actions}"
        return None

    def _ask(self, text: str, version: int, error: str | None = None) -> Waiting:
        return Waiting(key="approve_text", subject_version=version, payload={"text": text, "version": version},
                       actions=["approve", "edit", "reject"], decision_schema=EchoDecision.model_json_schema(),
                       view="generic.text_review", engine_key=f"draft-{version}", error=error)

    def _resume(self, task, ctx) -> Finished | Waiting:
        wp, answer = task.resume.waitpoint, task.resume.decision
        d = EchoDecision.model_validate(answer.body)
        text, version = wp.payload["text"], wp.subject_version
        ctx.step(f"review: {d.verdict}")
        if d.version != version:
            return self._ask(text, version,
                             error=f"版本不符：当前是 v{version}，提交的是 v{d.version}（请刷新后重新审核）")
        if d.verdict == "edit":
            return self._ask(d.text, version + 1)
        if d.verdict == "reject":
            return Finished(outcome="rejected_by_reviewer", summary={"reason": d.reason, "by": answer.by})
        return self._deliver(task, task.command, ctx, text, approved_by=answer.by, version=version)

    def _deliver(self, task, command, ctx, text: str, approved_by: str = "", version: int = 0) -> Finished:
        out = ctx.artifact_dir / "echo.txt"
        tmp = out.with_suffix(".part")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(out)
        summary = {"chars": len(text), "attempt": task.attempt, "command": command.kind}
        if approved_by or version:
            summary |= {"approved_by": approved_by, "version": version}
        return Finished(outcome="delivered", artifacts=[ArtifactOut(kind="text", path=out)], summary=summary)
