"""分镜生成 Agent (V2.0 §5.4): turn the words into shots, producing the Plan.

One shot per quote (slice) or script line (material), in the given order. The model decides each
shot's purpose, the picture it needs and the on-screen emphasis; code copies the words and
durations and checks the structure (hook first, call to action or ending last, no long run of the
same shot type, total length on target). Footage is chosen later by match_assets.
"""
from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import Ms, Plan, Purpose, SellingPoints, Shot, ShotType
from .base import Agent


class StoryItem(BaseModel):
    seq: int
    text: str
    duration_ms: Ms
    point: str = ""
    source_asset_id: str | None = None
    source_line_ids: list[int] = []
    live_shot_type: ShotType | None = None  # slice mode: what the replay shows at this moment


class StoryboardInput(BaseModel):
    run_id: str
    mode: Literal["slice", "material"]
    product: str
    category: str
    items: list[StoryItem]
    selling: SellingPoints
    target_ms: tuple[Ms, Ms]
    max_same_in_a_row: int = 2
    plan_version: int = 1
    rework_notes: list[str] = []  # findings from auto_review when this is a rework


class ShotAnswer(BaseModel):
    seq: int = Field(description="the item seq this shot carries")
    purpose: Purpose
    shot_type: ShotType = Field(description="the picture this line needs")
    text_effect: str = Field("", description="on-screen emphasis, e.g. 卖点关键词加粗弹跳：显瘦")
    transition: Literal["cut", "fade"] = "cut"


class StoryboardAnswer(BaseModel):
    """One shot per item, same order as the items."""

    shots: list[ShotAnswer] = Field(min_length=1)


class StoryboardAgent(Agent[StoryboardInput, StoryboardAnswer]):
    name = "storyboard"
    title = "分镜生成"
    prompt_id = "storyboard"
    input_model = StoryboardInput
    output_model = StoryboardAnswer
    tier = "creative"

    def check(self, inp, out):
        """Integrity: exactly one shot per item, in order (the words and durations are copied from them)."""
        if [s.seq for s in out.shots] != [i.seq for i in inp.items]:
            return [f"每条内容一个镜头，按原顺序：应为 {[i.seq for i in inp.items]}"]
        return []

    def quality(self, inp, out):
        problems = []
        if out.shots[0].purpose != "hook":
            problems.append("首镜 purpose 必须是 hook")
        if out.shots[-1].purpose not in ("cta", "ending"):
            problems.append("末镜 purpose 必须是 cta 或 ending")
        run, prev = 0, None
        for s in out.shots:
            run = run + 1 if s.shot_type == prev else 1
            prev = s.shot_type
            if run > inp.max_same_in_a_row:
                problems.append(f"镜头 {s.seq}：连续 {run} 个 {s.shot_type}，画面要交替")
        return problems


def to_plan(inp: StoryboardInput, out: StoryboardAnswer) -> Plan:
    items = {i.seq: i for i in inp.items}
    return Plan(run_id=inp.run_id, version=inp.plan_version, mode=inp.mode, product=inp.product,
                target_ms=inp.target_ms, shots=[
                    Shot(seq=s.seq, purpose=s.purpose, shot_type=s.shot_type, line=items[s.seq].text,
                         source_line_ids=items[s.seq].source_line_ids, source_asset_id=items[s.seq].source_asset_id,
                         caption=items[s.seq].text, text_effect=s.text_effect, duration_ms=items[s.seq].duration_ms,
                         transition=s.transition)
                    for s in out.shots
                ])
