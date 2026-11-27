"""Clipflow reads the library through the `AssetLibrary` interface (workers/base.py), so the graph
doesn't change. For a product the library knows nothing about yet, each question goes to the
fallback (the pilot's product sheets and shot tags), so jobs that worked before keep working.
"""
from dataclasses import dataclass

from ..contracts import AssetRange, Block, ProductBlocks, SourceVideo
from ..workers.pilot import _same_color
from .store import LibraryStore


@dataclass
class PostgresLibrary:
    store: LibraryStore
    fallback: object | None = None  # another AssetLibrary

    def replays_for(self, product: str) -> list[SourceVideo]:
        seen: dict[str, None] = {}
        for s in self.store.segments(product, kind="sell_block"):  # newest ingestion first
            seen.setdefault(s["path"], None)
        if seen:
            return [SourceVideo(asset_id=p) for p in seen]
        return self.fallback.replays_for(product) if self.fallback else []

    def linked_blocks(self, product: str, asset_id: str) -> ProductBlocks | None:
        """Only blocks a person confirmed; unconfirmed ones leave the search to locate_product."""
        rows = self.store.segments(product, kind="sell_block", asset_path=asset_id, confirmed_only=True)
        if not rows:
            return None
        blocks = [Block(start_ms=r["start_ms"], end_ms=r["end_ms"], confidence=r["confidence"] or "high",
                        evidence=r["evidence"] or [r["text"][:30] or "（人工确认）"])
                  for r in sorted(rows, key=lambda r: r["start_ms"])]
        return ProductBlocks(asset_id=asset_id, product=product, blocks=blocks)

    def find(self, product: str, colors: list[str], shot_type: str | None, min_ms: int) -> list[tuple[str, AssetRange]]:
        shots = self.store.segments(product, kind="shot")
        if not shots:
            return self.fallback.find(product, colors, shot_type, min_ms) if self.fallback else []
        out = []
        for s in shots:
            if shot_type and s["shot_type"] != shot_type:
                continue
            if colors and not (s["color"] and any(_same_color(s["color"], c) for c in colors)):
                continue
            if s["end_ms"] - s["start_ms"] < min(min_ms, 2000):
                continue
            color = next((c for c in colors if s["color"] and _same_color(s["color"], c)), s["color"])
            out.append((s["path"], AssetRange(start_ms=s["start_ms"], end_ms=s["end_ms"], shot_type=s["shot_type"],
                                              color=color, quality=s["quality"], confirmed=s["confirmed"])))
        return out

    def reference_images(self, product: str) -> list[str]:
        """Frames where ingestion saw this very product clearly."""
        refs = [f["path"] for s in self.store.segments(product, kind="shot") for f in s["frames"]
                if f.get("product_visible") == "clear" and f.get("same_item") == "yes" and f.get("path")]
        if refs:
            return refs[:2]
        return self.fallback.reference_images(product) if self.fallback else []
