"""Shared building blocks for the contracts.

Conventions (from the meeting brief, section 4):
- times are integer milliseconds (`*_ms`), never float seconds;
- ids are strings;
- every top-level contract carries `schema_version`, so stored artifacts can be migrated.
"""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Ms = Annotated[int, Field(ge=0, description="milliseconds")]


class Contract(BaseModel):
    """Base for every contract passed between steps or stored as an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1


class Part(BaseModel):
    """Base for nested parts of a contract (no own schema_version)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeRange(Part):
    start_ms: Ms
    end_ms: Ms

    @model_validator(mode="after")
    def _ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError(f"end_ms ({self.end_ms}) must be after start_ms ({self.start_ms})")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def overlaps(self, other: "TimeRange") -> bool:
        return self.start_ms < other.end_ms and other.start_ms < self.end_ms
