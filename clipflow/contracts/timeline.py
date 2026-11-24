"""The mechanical side: how the plan renders. Only code writes these.

`Timeline` is a pure function of Plan + assets + render settings, so it can always be rebuilt.
"""
from typing import Literal

from pydantic import Field

from .common import Contract, Ms, Part


class Canvas(Part):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    lufs: float = -14.0


class VideoItem(Part):
    shot_seq: int
    asset_id: str
    in_ms: Ms
    out_ms: Ms
    at_ms: Ms  # position on the timeline
    fit: Literal["cover", "contain"] = "cover"


class AudioItem(Part):
    kind: Literal["live", "voice", "bgm", "sfx"]
    asset_id: str
    in_ms: Ms
    out_ms: Ms
    at_ms: Ms
    gain_db: float = 0.0


class Caption(Part):
    start_ms: Ms
    end_ms: Ms
    text: str
    position: Literal["bottom", "top"] = "bottom"  # top while the product fills the lower half


class EndCard(Part):
    start_ms: Ms
    end_ms: Ms
    lines: list[str]


class Timeline(Contract):
    run_id: str
    plan_version: int
    version: int = 1
    canvas: Canvas = Canvas()
    video: list[VideoItem] = Field(min_length=1)
    audio: list[AudioItem] = []
    captions: list[Caption] = []
    end_card: EndCard | None = None

    @property
    def duration_ms(self) -> int:
        ends = [v.at_ms + (v.out_ms - v.in_ms) for v in self.video]
        if self.end_card:
            ends.append(self.end_card.end_ms)
        return max(ends)


class VoiceTrack(Contract):
    """tts output: one audio clip per script line, with real durations."""

    run_id: str
    clips: list[AudioItem]


class RenderResult(Contract):
    run_id: str
    timeline_version: int
    path: str
    reused_shots: list[int] = []  # shots taken from the per-shot cache (partial re-render)


class Measured(Part):
    duration_ms: Ms
    fps: float
    width: int
    height: int
    lufs: float | None = None
    peak_db: float | None = None


class QCCheck(Part):
    id: str
    severity: Literal["fail", "warn"]
    at_ms: Ms | None = None
    detail: str
    # who can fix a failed check: system = rebuild/re-render automatically; human = the reviewer
    # (e.g. pick footage for an empty shot); admin = a technical fault, the run stops as failed
    fix_by: Literal["system", "human", "admin"] = "admin"


class QCReport(Contract):
    run_id: str
    timeline_version: int
    passed: bool
    checks: list[QCCheck] = []
    measured: Measured
