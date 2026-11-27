"""Interfaces for long-running media work: ASR, frame sampling, TTS, rendering, measuring.

Nodes call these interfaces, never ffmpeg or Whisper directly. Today an implementation runs the
work in-process (and blocks the node). Later the same interface can submit a job to a GPU worker
queue and wait for it, without changing the graph. That is the answer to the brief's question 3.
"""
from typing import Protocol

from ..contracts import (
    AssetRange, AudioItem, Measured, Product, ProductBlocks, RenderResult, ScriptLine, SourceVideo, TimeRange,
    Timeline, Transcript,
)
from ..agents.vision import Frame


class ASR(Protocol):
    def transcribe(self, asset_id: str, window: TimeRange | None = None) -> Transcript:
        """Line-level transcript. Cached by asset hash + window (about 8 min per hour of footage)."""
        ...

    def clauses(self, asset_id: str, ranges: list[TimeRange]) -> Transcript:
        """Re-transcribe only these ranges with word timestamps and split into clauses."""
        ...


class FrameSampler(Protocol):
    def sample(self, asset_id: str, ranges: list[TimeRange], every_ms: int) -> list[Frame]: ...


class TTS(Protocol):
    def speak(self, run_id: str, lines: list[ScriptLine], voice: str | None) -> list[AudioItem]:
        """One clip per line; the real durations go back into the Plan."""
        ...


class Renderer(Protocol):
    def render(self, timeline: Timeline, previous: RenderResult | None = None) -> RenderResult:
        """Render the timeline. With `previous`, reuse unchanged shots (partial re-render)."""
        ...

    def measure(self, path: str) -> Measured: ...


class Catalog(Protocol):
    """Product sheets by 款名 (products/<款名>.yaml in the pilot; the product table later)."""

    def get(self, product_id: str) -> Product:
        """Raises KeyError for an unknown product."""
        ...

    def ids(self) -> list[str]:
        """Every product id (款名) in the catalog."""
        ...


class AssetLibrary(Protocol):
    """What ingestion (outside this graph) has filed: replays and product footage, with tags.

    This is the boundary to agree with the architect: `product id → linked replay blocks →
    shot candidates`. The graph only reads it; ingestion fills it on its own schedule.
    """

    def replays_for(self, product: str) -> list[SourceVideo]:
        """Replays ingestion has linked to this product (newest first). Empty = none known."""
        ...

    def linked_blocks(self, product: str, asset_id: str) -> ProductBlocks | None:
        """Blocks ingestion already attributed (and a person confirmed) for this product in this
        replay. None = not attributed yet: locate_product finds them itself."""
        ...

    def find(self, product: str, colors: list[str], shot_type: str | None, min_ms: int) -> list[tuple[str, AssetRange]]:
        """(asset_id, tagged range) candidates, best first."""
        ...

    def reference_images(self, product: str) -> list[str]: ...


class CaseLibrary(Protocol):
    """Top-video breakdowns (爆款拆解素材库, 优秀切片文案库)."""

    def similar(self, category: str, price: float | None, focus: list[str] = (), hook_style: str = "any",
                k: int = 3) -> list[dict]:
        """Cases as {case_id, summary, hook_type}. Today: category + price + focus words + hook style."""
        ...
