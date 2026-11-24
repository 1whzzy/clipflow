"""反馈归因 Agent (V2.0 §5.6): turn reviewers' edits into concrete improvement actions.

Offline (a daily batch), never on the run's path. Code first maps every edit to the node and field
it changed and counts patterns; the model only explains the patterns and proposes an action, citing
sample run ids, so a conclusion can always be traced back to real runs.
"""
from typing import Literal

from pydantic import BaseModel, Field

from .base import Agent


class EditPattern(BaseModel):
    node: str  # e.g. "select_quotes", "match_assets"
    field: str  # e.g. "caption", "asset"
    count: int
    run_ids: list[str]
    examples: list[str]  # short before → after descriptions


class FeedbackInput(BaseModel):
    period: str  # e.g. "2026-11-24"
    patterns: list[EditPattern]
    prompt_versions: dict[str, str] = {}  # node → prompt ref in use


class Attribution(BaseModel):
    node: str
    field: str
    cause: Literal["prompt", "retrieval", "contract", "rule", "footage", "unclear"]
    explanation: str
    action: Literal["edit_prompt", "add_cases", "adjust_threshold", "add_rule", "reshoot", "none"]
    sample_run_ids: list[str] = Field(min_length=1)


class FeedbackAnswer(BaseModel):
    """One attribution per pattern worth acting on."""

    attributions: list[Attribution]


class FeedbackAgent(Agent[FeedbackInput, FeedbackAnswer]):
    name = "feedback"
    title = "反馈归因"
    prompt_id = "feedback"
    input_model = FeedbackInput
    output_model = FeedbackAnswer
    tier = "reasoning"

    def check(self, inp, out):
        known = {(p.node, p.field): set(p.run_ids) for p in inp.patterns}
        problems = []
        for a in out.attributions:
            if (a.node, a.field) not in known:
                problems.append(f"{a.node}.{a.field} 不是输入里的修改模式")
            elif bad := set(a.sample_run_ids) - known[(a.node, a.field)]:
                problems.append(f"{a.node}.{a.field}：样本 {sorted(bad)} 不在该模式的 run 里")
        return problems
