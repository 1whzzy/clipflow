"""Human decisions at the two pauses, and the final delivery."""
from typing import Literal

from pydantic import model_validator

from .common import Contract, Part

EditKind = Literal["caption", "asset", "cut", "delete", "order", "music", "cover"]


class Edit(Part):
    kind: EditKind
    seq: int | None = None  # which shot; not used for order / music / cover
    value: str | int | list[int] | dict | None = None

    @model_validator(mode="after")
    def _shape(self):
        if self.kind in ("caption", "asset", "cut", "delete") and self.seq is None:
            raise ValueError(f"a {self.kind} edit needs seq")
        if self.kind == "order" and not (isinstance(self.value, list) and self.value):
            raise ValueError("an order edit needs value = the full list of shot seqs in the new order")
        if self.kind == "caption" and not (isinstance(self.value, str) and self.value.strip()):
            raise ValueError("a caption edit needs the new text")
        if self.kind == "cut" and not (isinstance(self.value, dict) and {"in_ms", "out_ms"} <= set(self.value)):
            raise ValueError("a cut edit needs value = {in_ms, out_ms}")
        return self

    @property
    def changes_text(self) -> bool:
        """Text edits go back through auto_review (banned words, prices) before re-rendering."""
        return self.kind == "caption"


class Decision(Contract):
    stage: Literal["plan", "video"]
    verdict: Literal["approve", "edit", "reject"]
    version: int  # the Plan (stage=plan) or Timeline (stage=video) version the reviewer looked at
    edits: list[Edit] = []
    reason: str = ""  # required when approving with waivable findings still open
    by: str = ""
    at: str = ""  # ISO 8601

    @model_validator(mode="after")
    def _edits_match_verdict(self):
        if self.verdict == "edit" and not self.edits:
            raise ValueError("verdict=edit needs at least one edit")
        if self.verdict != "edit" and self.edits:
            raise ValueError("edits are only allowed with verdict=edit")
        return self


class Delivery(Contract):
    run_id: str
    video_path: str
    plan_version: int
    timeline_version: int
    qc_passed: bool
    waived: list[str] = []  # findings a reviewer accepted, with their reason
    cover_path: str | None = None
    caption_file: str | None = None
