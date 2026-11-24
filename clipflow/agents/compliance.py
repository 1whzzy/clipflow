"""合规审核 Agent (V2.0 §5.5): the semantic half of auto_review. It only judges; it never rewrites.

Two channels: code rules run first (banned words, prices, unverified claims, structure), then the
model looks for what rules can't see (exaggerated effects, medical hints, misleading comparisons,
pushy wording). The final verdict is computed in code, so the model can never overrule a rule hit.
"""
from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import Finding, Product, Review
from .base import Agent


class ReviewShot(BaseModel):
    seq: int
    purpose: str
    line: str
    caption: str


class ComplianceInput(BaseModel):
    product: Product
    plan_version: int
    shots: list[ReviewShot]
    rule_findings: list[Finding] = []  # already found by code; shown so the model doesn't repeat them


class LlmFinding(BaseModel):
    seq: int
    kind: Literal["exaggeration", "medical", "comparison", "pushy", "mismatch", "other"]
    severity: Literal["fix", "note"]
    detail: str
    suggestion: str = ""


class ComplianceAnswer(BaseModel):
    """Problems the rules did not catch. Empty list if there are none."""

    findings: list[LlmFinding] = Field(default=[])


class ComplianceAgent(Agent[ComplianceInput, ComplianceAnswer]):
    name = "compliance"
    title = "合规审核"
    prompt_id = "compliance"
    input_model = ComplianceInput
    output_model = ComplianceAnswer
    tier = "reasoning"

    def check(self, inp, out):
        seqs = {s.seq for s in inp.shots}
        return [f"镜头 {f.seq} 不存在" for f in out.findings if f.seq not in seqs]


def verdict(plan_version: int, rule_findings: list[Finding], answer: ComplianceAnswer | None) -> Review:
    llm = [Finding(rule_id=f"llm.{f.kind}", severity=f.severity, seq=f.seq, detail=f.detail,
                   suggestion=f.suggestion, fix_at="content") for f in (answer.findings if answer else [])]
    findings = rule_findings + llm
    severities = {f.severity for f in findings}
    if "block" in severities:
        v = "hard_fail"
    elif "fix" in severities:
        v = "revise"
    elif findings or answer is None:  # answer None = the model step was unavailable: pass, but flag it
        v = "pass_with_flag"
    else:
        v = "pass"
    return Review(plan_version=plan_version, verdict=v, findings=findings)
