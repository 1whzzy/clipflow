"""Small helpers shared by the nodes."""
import uuid
from typing import TypeVar

from pydantic import BaseModel

from ..agents import AgentResult
from ..contracts import Review, RunRequest
from ..graph.context import RunContext
from ..graph.state import RunState

M = TypeVar("M", bound=BaseModel)


def load(ctx: RunContext, state: RunState, key: str, model: type[M]) -> M:
    return ctx.store.get(state[key], model)


def request(ctx: RunContext, state: RunState) -> RunRequest:
    return load(ctx, state, "request", RunRequest)


def trace(node: str, result: AgentResult) -> dict:
    """One trace entry per agent call: enough to find the prompt version and cost of any output."""
    return {
        "id": uuid.uuid4().hex[:12], "node": node, "prompt": result.prompt, "cached": result.cached,
        "attempts": len(result.attempts),
        "problems": [p for a in result.attempts for p in a.problems][:10],
        "model": result.attempts[-1].model if result.attempts else None,
        "tokens_in": sum(a.tokens_in for a in result.attempts),
        "tokens_out": sum(a.tokens_out for a in result.attempts),
        "seconds": round(sum(a.seconds for a in result.attempts), 1),
        "warnings": result.warnings,
    }


def warning_flags(node: str, result: AgentResult) -> list[str]:
    """Quality rules an agent's best attempt still breaks; shown to the reviewer at both pauses."""
    return [f"{node}_warning:{w}" for w in result.warnings]


def rework_notes(ctx: RunContext, state: RunState, fix_at: str | None = None) -> list[str]:
    """The latest review's must-fix findings, as notes for the agent doing the rework."""
    if not state.get("review"):
        return []
    review = load(ctx, state, "review", Review)
    if review.verdict != "revise":
        return []
    return [f"镜头 {f.seq}：{f.detail}" + (f"（建议：{f.suggestion}）" if f.suggestion else "")
            for f in review.findings if f.severity == "fix" and (fix_at is None or f.fix_at == fix_at)]
