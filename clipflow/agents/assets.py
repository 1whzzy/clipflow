"""素材召回 Agent (V2.0 §5.3): choose footage for one shot from candidates code already filtered.

Code does the hard filtering (product, color, shot type, length, quality) from the asset library's
tags; the model only ranks what is left against the line being said. No candidates → no model
call: the shot goes on the reshoot list. It never writes copy and never blocks the run.
"""
from pydantic import BaseModel, Field

from ..contracts import ClipRef, Ms, ShotType
from .base import Agent


class Candidate(BaseModel):
    index: int
    asset_id: str
    in_ms: Ms
    out_ms: Ms
    shot_type: ShotType | None = None
    color: str | None = None
    note: str = ""


class AssetInput(BaseModel):
    seq: int
    line: str
    purpose: str
    shot_type: ShotType
    need_ms: Ms
    colors: list[str]
    candidates: list[Candidate] = Field(min_length=1)


class AssetAnswer(BaseModel):
    """The best candidate for this shot, plus up to 3 alternates for the reviewer."""

    choice: int
    alternates: list[int] = Field(default=[], max_length=3)
    reason: str = ""


class AssetAgent(Agent[AssetInput, AssetAnswer]):
    name = "assets"
    title = "素材召回"
    prompt_id = "assets"
    input_model = AssetInput
    output_model = AssetAnswer
    tier = "fast"

    def check(self, inp, out):
        valid = {c.index for c in inp.candidates}
        picked = [out.choice, *out.alternates]
        problems = [f"候选 {i} 不存在" for i in picked if i not in valid]
        if len(picked) != len(set(picked)):
            problems.append("首选和备选不能重复")
        return problems


def to_clips(inp: AssetInput, out: AssetAnswer) -> tuple[ClipRef, list[ClipRef]]:
    by = {c.index: c for c in inp.candidates}

    def clip(i: int) -> ClipRef:
        c = by[i]
        return ClipRef(asset_id=c.asset_id, in_ms=c.in_ms, out_ms=min(c.out_ms, c.in_ms + inp.need_ms))

    return clip(out.choice), [clip(i) for i in out.alternates]
