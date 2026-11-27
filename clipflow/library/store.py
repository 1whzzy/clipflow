"""The material library's tables (migrations/), and the one place that writes and reads them.

Writes come only from the `ingest` workflow, one file at a time: `publish` replaces what an earlier
ingestion of the same file said about the same products, in one transaction, so running it twice
(a retry, a re-scan) leaves the same rows.
"""
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator
from psycopg.types.json import Jsonb

MIGRATIONS = sorted(Path(__file__).with_name("migrations").glob("[0-9][0-9][0-9]_*.sql"))


class Segment(BaseModel):
    """A stretch of a file, for one product."""

    model_config = ConfigDict(extra="forbid")
    product: str
    kind: Literal["sell_block", "shot"]
    start_ms: int
    end_ms: int
    shot_type: str | None = None
    color: str | None = None
    quality: Literal["good", "ok", "bad"] | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    evidence: list[str] = []
    text: str = ""
    frames: list[dict] = []
    confirmed: bool = False
    confirmed_by: str = ""

    @model_validator(mode="after")
    def _ordered(self):
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError(f"bad range {self.start_ms}–{self.end_ms} ms")
        return self


class AssetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    kind: Literal["replay", "footage"]
    bytes: int | None = None
    mtime: float | None = None
    duration_ms: int | None = None
    product: str | None = None
    color: str | None = None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class LibraryStore:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    def open(cls, url: str, schema: str = "library") -> LibraryStore:
        from foundation import db

        pool = db.open_pool(url, schema, max_size=4)
        db.check(pool, MIGRATIONS)
        return cls(pool)

    # ---- writes (the ingest workflow) ----------------------------------------------------------

    def publish(self, asset: AssetIn, segments: list[Segment], products: list[str], task_id: str,
                by: str = "", status: Literal["processed", "rejected"] = "processed") -> str:
        """Record the file and what was found in it for `products`, replacing an earlier ingestion's
        segments for those products. Returns the asset's id."""
        assert all(s.product in products for s in segments), "a segment for a product that wasn't processed"
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO assets (id, path, kind, bytes, mtime, duration_ms, product, color, status, task_id,
                                       processed_by, processed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (path) DO UPDATE SET kind = excluded.kind, bytes = excluded.bytes,
                       mtime = excluded.mtime, duration_ms = excluded.duration_ms, product = excluded.product,
                       color = excluded.color, status = excluded.status, task_id = excluded.task_id,
                       processed_by = excluded.processed_by, processed_at = now()
                   RETURNING id""",
                (new_id("as"), asset.path, asset.kind, asset.bytes, asset.mtime, asset.duration_ms, asset.product,
                 asset.color, status, task_id, by)).fetchone()
            asset_id = row["id"]
            conn.execute("DELETE FROM segments WHERE asset_id = %s AND product = ANY(%s)", (asset_id, products))
            for s in segments:
                conn.execute(
                    """INSERT INTO segments (id, asset_id, product, kind, start_ms, end_ms, shot_type, color, quality,
                                             confidence, evidence, text, frames, confirmed, confirmed_by, task_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (new_id("seg"), asset_id, s.product, s.kind, s.start_ms, s.end_ms, s.shot_type, s.color,
                     s.quality, s.confidence, Jsonb(s.evidence), s.text, Jsonb(s.frames), s.confirmed,
                     s.confirmed_by, task_id))
            return asset_id

    def register(self, asset: AssetIn, task_id: str) -> str:
        """Make the file known (status `pending` if it is new) without touching what an earlier
        ingestion filed, so it can be played while a person reviews it. Returns its id."""
        with self.pool.connection() as conn:
            return conn.execute(
                """INSERT INTO assets (id, path, kind, bytes, mtime, duration_ms, product, color, status, task_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                   ON CONFLICT (path) DO UPDATE SET path = excluded.path
                   RETURNING id""",
                (new_id("as"), asset.path, asset.kind, asset.bytes, asset.mtime, asset.duration_ms, asset.product,
                 asset.color, task_id)).fetchone()["id"]

    # ---- reads -----------------------------------------------------------------------------------

    def products(self) -> list[dict]:
        """Every product with something in the library, and how much."""
        with self.pool.connection() as conn:
            return conn.execute(
                """SELECT product,
                          count(*) FILTER (WHERE kind = 'sell_block') AS sell_blocks,
                          count(*) FILTER (WHERE kind = 'shot') AS shots,
                          count(DISTINCT asset_id) AS files
                   FROM segments GROUP BY product ORDER BY product""").fetchall()

    def segments(self, product: str, kind: str | None = None, asset_path: str | None = None,
                 confirmed_only: bool = False, limit: int = 500) -> list[dict]:
        """A product's segments with their file, best first: confirmed, then good quality, then longer."""
        with self.pool.connection() as conn:
            return conn.execute(
                """SELECT s.*, a.path, a.kind AS asset_kind, a.duration_ms AS asset_duration_ms,
                          a.processed_at
                   FROM segments s JOIN assets a ON a.id = s.asset_id
                   WHERE s.product = %(p)s AND a.status = 'processed'
                     AND (%(k)s::text IS NULL OR s.kind = %(k)s)
                     AND (%(path)s::text IS NULL OR a.path = %(path)s)
                     AND (NOT %(c)s OR s.confirmed)
                   ORDER BY s.confirmed DESC, (s.quality = 'good') DESC NULLS LAST,
                            a.processed_at DESC, a.path, s.start_ms
                   LIMIT %(n)s""",
                {"p": product, "k": kind, "path": asset_path, "c": confirmed_only, "n": limit}).fetchall()

    def asset(self, asset_id: str) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM assets WHERE id = %s", (asset_id,)).fetchone()

    def segment(self, segment_id: str) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM segments WHERE id = %s", (segment_id,)).fetchone()

    def asset_by_path(self, path: str) -> dict | None:
        with self.pool.connection() as conn:
            return conn.execute("SELECT * FROM assets WHERE path = %s", (path,)).fetchone()
