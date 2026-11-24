"""Conditional edges: every branch and loop of the workflow, in one place.

Each function reads the state (and the latest Review / QCReport) and returns the next node's name.
"""
from langgraph.graph import END
from langgraph.runtime import Runtime

from ..contracts import Decision, QCReport, Review
from .context import RunContext
from .state import RunState


def after_route(state: RunState) -> str:
    """2 → 3 or 8: the only place the two modes split."""
    return "slice_front" if state["mode"] == "slice" else "prepare_script"


def slice_entry(state: RunState) -> str:
    """Entry of the slice_front subgraph. A content rework skips the media steps already done."""
    return "select_quotes" if state.get("clauses") and state.get("quotes") else "transcribe"


def after_locate(state: RunState) -> str:
    """4 → 5, or 4 again with the user's time range, or stop when the user cancels."""
    if state.get("status") == "cancelled":
        return END
    return "refine_words" if state.get("blocks") else "locate_product"


def after_slice_front(state: RunState) -> str:
    return END if state.get("status") == "cancelled" else "storyboard"


def after_review(state: RunState, runtime: Runtime[RunContext]) -> str:
    """11 → stop / rework / continue. Rework goes back to where the problem was made:
    content problems to the words (7 or 8), storyboard problems to 9, footage problems to 10.
    After `limits.review_rounds` reworks the best version seen goes on to a human, flagged
    (auto_review has already swapped it in)."""
    ctx = runtime.context
    review = ctx.store.get(state["review"], Review)
    if review.verdict == "hard_fail":
        return END
    human_edit = state.get("plan_by") == "human"  # never let an agent overwrite a reviewer's edits
    if review.verdict == "revise" and not human_edit and state.get("review_round", 0) <= ctx.limits.review_rounds:
        targets = {f.fix_at for f in review.findings if f.severity == "fix"}
        if "content" in targets:
            return "slice_front" if state["mode"] == "slice" else "prepare_script"
        if "storyboard" in targets:
            return "storyboard"
        return "match_assets"
    # pass, pass_with_flag, reworks used up, or a human edit: back to the human; must-fix findings
    # block approval there (nodes/_human.py)
    # (confirm_plan passes straight through when confirmation is turned off for the run)
    return "build_timeline" if state.get("phase") == "video" else "confirm_plan"


def after_confirm(state: RunState, runtime: Runtime[RunContext]) -> str:
    """12 → 13 (material) or 14 (slice); an edited plan is re-checked first; reject ends the run."""
    if state.get("status") == "rejected":
        return END
    if state.get("plan_decision"):
        decision = runtime.context.store.get(state["plan_decision"], Decision)
        if decision.verdict == "edit":
            return "auto_review"
    return "tts" if state["mode"] == "material" else "build_timeline"


def after_qc(state: RunState, runtime: Runtime[RunContext]) -> str:
    """16 → 14 when the system can fix it (rebuild, re-render); → end when the run failed technically
    (qc set status=failed); → 17 otherwise. Failed checks left for the reviewer block approval."""
    if state.get("status") == "failed":
        return END
    report = runtime.context.store.get(state["qc"], QCReport)
    if any(c.fix_by == "system" for c in report.checks if c.severity == "fail"):
        return "build_timeline"
    return "review_video"


def after_video_review(state: RunState, runtime: Runtime[RunContext]) -> str:
    """17 → 18 (approve), 11 (text edited: re-check), 14 (picture/cut edits), or end."""
    if state.get("status") in ("rejected", "stopped"):
        return END
    decision = runtime.context.store.get(state["video_decision"], Decision)
    if decision.verdict == "approve":
        return "deliver"
    return "auto_review" if any(e.changes_text for e in decision.edits) else "build_timeline"
