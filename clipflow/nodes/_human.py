"""The two human pauses share one rule set: what a reviewer may submit, and what blocks approval.

A submitted decision is checked before the run moves on. If it is invalid (wrong version, unknown
shot, approving while a must-fix problem is open, ...) the run pauses again with the error, instead
of continuing on a decision nobody meant. Calling `interrupt` several times in one node is safe:
on resume LangGraph replays earlier answers in order.
"""
from pydantic import ValidationError
from langgraph.types import interrupt

from ..contracts import Decision, Plan, QCReport, Review, Timeline
from ..graph.context import RunContext
from ..graph.state import RunState
from ..rules import MUST_FIX
from ._util import load


def blocking_issues(ctx: RunContext, state: RunState, plan: Plan, stage: str) -> list[str]:
    """Problems that must be fixed before this plan/video may be approved or delivered."""
    issues = []
    if state.get("review"):
        review = load(ctx, state, "review", Review)
        if review.plan_version == plan.version:
            issues += [f"镜头 {f.seq}：{f.detail}" for f in review.findings
                       if f.rule_id in MUST_FIX and f.severity in ("block", "fix")]
    if stage == "video" and state.get("qc"):
        report = load(ctx, state, "qc", QCReport)
        timeline = load(ctx, state, "timeline", Timeline)
        if report.timeline_version == timeline.version:
            issues += [f"质检 {c.id}：{c.detail}" for c in report.checks if c.severity == "fail"]
    return issues


def waivable_findings(ctx: RunContext, state: RunState, plan: Plan) -> list[str]:
    if not state.get("review"):
        return []
    review = load(ctx, state, "review", Review)
    if review.plan_version != plan.version:
        return []
    return [f"镜头 {f.seq}：{f.detail}" for f in review.findings if f.rule_id not in MUST_FIX and f.severity == "fix"]


def ask(stage: str, payload: dict, version: int, plan: Plan, blocking: list[str], waivable: list[str],
        allow_edit: bool = True) -> Decision:
    actions = ["approve", "edit", "reject"] if allow_edit else ["approve", "reject"]
    if blocking:
        actions.remove("approve")
    error = None
    while True:
        raw = interrupt({**payload, "kind": f"{'confirm_plan' if stage == 'plan' else 'review_video'}",
                         "version": version, "actions": actions, "must_fix": blocking, "may_waive": waivable,
                         "error": error})
        try:
            decision = Decision.model_validate({"stage": stage, **raw})
        except ValidationError as e:
            error = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
            continue
        error = _problem(decision, version, plan, actions, blocking, waivable)
        if error is None:
            return decision


def _problem(d: Decision, version: int, plan: Plan, actions: list[str], blocking: list[str],
             waivable: list[str]) -> str | None:
    if d.version != version:
        return f"版本不符：当前是 v{version}，提交的是 v{d.version}（请刷新后重新审核）"
    if d.verdict not in actions:
        if d.verdict == "approve" and blocking:
            return "以下问题必须先修改，不能直接批准：" + "；".join(blocking)
        return f"现在只能选择 {actions}"
    if d.verdict == "approve" and waivable and not d.reason.strip():
        return "仍有未解决的提示项，批准时请填写理由：" + "；".join(waivable)
    seqs = {s.seq for s in plan.shots}
    remaining = set(seqs)  # edits apply in order: a reorder after a delete lists the shots left
    for e in d.edits:
        if e.seq is not None and e.seq not in remaining:
            return f"镜头 {e.seq} 不存在（现有 {sorted(remaining)}）"
        if e.kind == "delete":
            remaining.discard(e.seq)
        if e.kind == "order" and sorted(e.value) != sorted(remaining):
            return f"重排必须列出全部镜头且不重复：现有 {sorted(remaining)}"
        if e.kind == "asset" and isinstance(e.value, int):
            shot = next(s for s in plan.shots if s.seq == e.seq)
            if not 0 <= e.value < len(shot.alternates):
                return f"镜头 {e.seq} 没有第 {e.value} 个备选素材"
    if not remaining:
        return "不能删除全部镜头"
    return None
