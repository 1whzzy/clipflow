"""The editorial side: what is said and shown, in which order, and why.

`Plan` is the seam between the two modes. Everything before it is mode-specific; everything after
it is shared. Humans confirm and edit at this level. LLMs write it; code checks it.
"""
from typing import Literal

from pydantic import Field

from .common import Contract, Ms, Part
from .media import ShotType

Purpose = Literal["hook", "selling_point", "proof", "price", "cta", "ending"]


# ---- slice mode: the anchor's own lines -------------------------------------------------------

class Quote(Part):
    seq: int
    asset_id: str
    line_ids: list[int] = Field(min_length=1, description="whole lines/clauses, in order")
    text: str  # must equal the transcript text of those lines
    role: Literal["opening", "middle", "ending"]
    point: str  # which selling point this quote carries
    hook_type: str = ""  # 痛点 / 福利价格 / 效果展示 / 悬念提问 / 场景 ... (opening only)
    reason: str = ""


class QuoteList(Contract):
    product: str
    quotes: list[Quote] = Field(min_length=1)


# ---- material mode: the script, split into lines ----------------------------------------------

class ScriptLine(Part):
    seq: int
    text: str
    role: Literal["opening", "middle", "ending"]
    point: str = ""


class ScriptLines(Contract):
    product: str
    script_version: int
    lines: list[ScriptLine] = Field(min_length=1)


# ---- selling points ----------------------------------------------------------------------------

class SellingPoint(Part):
    point: str
    priority: int = Field(ge=1)
    weight: Literal["main", "support"]
    evidence: list[str] = Field(min_length=1, description="case ids or rule ids that back the order")
    content_refs: list[int] = []  # quote/line seq numbers that carry this point


class SellingPoints(Contract):
    product: str
    category: str
    items: list[SellingPoint] = Field(min_length=1)
    reason: str = ""


# ---- the plan ----------------------------------------------------------------------------------

class ClipRef(Part):
    asset_id: str
    in_ms: Ms
    out_ms: Ms


class Speech(ClipRef):
    """Where this shot's words are heard from. It travels with the shot, so deleting or reordering
    shots can never give a shot another shot's voice."""

    kind: Literal["live", "voice"]  # live = the anchor in the replay; voice = TTS (material mode)


class Shot(Part):
    seq: int
    purpose: Purpose
    shot_type: ShotType
    line: str  # what is said (anchor's words or the script line)
    source_line_ids: list[int] = []  # slice mode: where the words come from
    source_asset_id: str | None = None
    caption: str  # the words shown; build_timeline splits them into on-screen captions (rules.caption_cue_chars)
    text_effect: str = ""
    duration_ms: Ms
    clip: ClipRef | None = None  # the picture; filled by match_assets
    speech: Speech | None = None  # the words' audio; kept when a product shot covers the picture
    alternates: list[ClipRef] = Field(default=[], max_length=3)
    transition: Literal["cut", "fade"] = "cut"


Severity = Literal["block", "fix", "note"]


class Finding(Part):
    rule_id: str  # e.g. "rule.opening_connective" or "llm.exaggeration"
    severity: Severity  # block = hard stop; fix = must be fixed; note = shown to the reviewer
    seq: int | None = None  # which shot, when it is about one
    detail: str
    fix_at: Literal["content", "storyboard", "assets"] = "storyboard"  # where a rework should go back to
    suggestion: str = ""


class Plan(Contract):
    run_id: str
    version: int = 1
    mode: Literal["slice", "material"]
    product: str
    target_ms: tuple[Ms, Ms]
    shots: list[Shot] = Field(min_length=1)
    findings: list[Finding] = []
    risks: list[str] = []
    gaps: list[str] = []  # shots with no usable footage → a reshoot list

    @property
    def total_ms(self) -> int:
        return sum(s.duration_ms for s in self.shots)


class Review(Contract):
    """auto_review's verdict on one Plan version. Only judges; never rewrites."""

    plan_version: int
    verdict: Literal["pass", "pass_with_flag", "revise", "hard_fail"]
    findings: list[Finding] = []
