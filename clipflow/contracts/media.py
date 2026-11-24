"""Footage and what the system learns from it: transcripts, product blocks, frame tags."""
from typing import Literal

from pydantic import Field

from .common import Contract, Ms, Part, TimeRange

ShotType = Literal[
    # From the V1.0 workflow design (node 7) plus the SOP's footage types.
    "look_half", "look_full", "detail_fabric", "detail_color", "detail_fit", "detail_match",
    "scene_lifestyle", "flat_lay", "hanging", "talking_head", "other",
]


class AssetRange(TimeRange):
    shot_type: ShotType | None = None
    color: str | None = None
    quality: Literal["good", "ok", "bad"] | None = None
    confirmed: bool = False  # a human checked this tag


class Asset(Contract):
    asset_id: str
    type: Literal["replay", "footage", "bgm"]
    path: str
    hash: str
    product: str | None = None
    color: str | None = None
    anchor: str | None = None
    date: str | None = None
    duration_ms: Ms
    width: int
    height: int
    has_audio: bool
    ranges: list[AssetRange] = []


class Word(Part):
    w: str
    start_ms: Ms
    end_ms: Ms


class Line(Part):
    i: int = Field(description="line number, stable within one transcript")
    start_ms: Ms
    end_ms: Ms
    text: str
    words: list[Word] | None = None


class Transcript(Contract):
    asset_id: str
    range: TimeRange | None = None  # None = whole file
    model: str
    lines: list[Line]


class Block(TimeRange):
    """A stretch of the replay where the anchor sells this product."""

    confidence: Literal["high", "medium", "low"]
    evidence: list[str] = Field(min_length=1, description="exact quotes from the transcript")
    line_ids: list[int] = []
    link_no: int | None = None


class ProductBlocks(Contract):
    asset_id: str
    product: str
    blocks: list[Block]  # empty = not found → the run pauses and asks for a time range


class FrameTag(Part):
    asset_id: str
    at_ms: Ms
    shot_type: ShotType
    product_visible: Literal["clear", "partial", "none"]
    same_item: Literal["yes", "no", "unsure"]
    color: str | None = None
    action: str | None = None  # standing, walking, close-up hand, ...
    note: str = ""


class FrameTags(Contract):
    product: str
    tags: list[FrameTag]
