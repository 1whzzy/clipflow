"""What a user submits: one job = one product + footage → one finished video.

A job can name the product by id (款名) only; intake then loads the product sheet from the catalog
(products/<款名>.yaml today). Slice jobs may also leave out the replay; intake then asks the asset
library for replays already linked to the product by ingestion.
"""
import re
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .common import Contract, Ms, Part

Mode = Literal["slice", "material"]


def parse_ms(value) -> int:
    """Accept milliseconds or a time a person would type: '3:56', '00:03:56', '236.5s'."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?s", text):
        return int(float(text[:-1]) * 1000)
    if re.fullmatch(r"\d+", text):
        return int(text)
    parts = text.split(":")
    if 2 <= len(parts) <= 3 and all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        seconds = 0.0
        for p in parts:
            seconds = seconds * 60 + float(p)
        return int(seconds * 1000)
    raise ValueError(f"not a time: {value!r} (use ms, 'm:ss' or 'h:mm:ss')")


class Product(Part):
    """The product sheet. The only source of truth for prices and claims in the video."""

    name: str = Field(description="款名, the join key across the shared-drive folders")
    category: str
    price: float | None = None
    price_verified: bool = False  # the video may state the price only when True
    colors: list[str] = []
    sizes: list[str] = []
    selling_points: list[str] = []
    verified_claims: list[str] = []  # material/craft words confirmed for this product; empty = none may be said
    aliases: list[str] = []  # how ASR tends to write the name
    link_no: int | None = None  # 直播间链接号, when known
    anchor: str | None = None


class SourceVideo(Part):
    asset_id: str
    start_ms: Ms | None = None  # optional rough time range, e.g. from the user
    end_ms: Ms | None = None

    @field_validator("start_ms", "end_ms", mode="before")
    @classmethod
    def _time(cls, v):
        return None if v is None else parse_ms(v)


class ScriptInput(Part):
    text: str
    status: Literal["draft", "final"]  # final: split into lines only, never reworded
    version: int = 1


class RunOptions(Part):
    target_ms: tuple[Ms, Ms] = (30_000, 45_000)
    max_shots: int = 12
    confirm_plan: bool = True  # pause for a human to confirm the plan (can be turned off)
    allow_substitute: bool = False  # may use footage of another color when the right one is missing
    voice: str | None = None  # TTS voice, material mode only
    bgm: str | None = None
    cta: str = ""  # end-card call to action; empty = none
    focus: list[str] = []  # selling points the user wants this video to lead with (e.g. 显瘦, 不贴腿)
    hook_style: str = "any"  # 痛点 / 福利价格 / 效果展示 / 悬念提问 / 场景, or any
    hook_keywords: list[str] = []  # the first sentence must say one of these
    must_mention: list[str] = []


class RunRequest(Contract):
    run_id: str
    mode: Mode
    product_id: str | None = None  # 款名; enough on its own, intake loads the sheet
    product: Product | None = None  # filled by intake when only product_id is given
    source_videos: list[SourceVideo] = []  # slice mode: the replay(s); empty = use the ones ingestion linked
    product_shots: list[str] = []  # asset ids or folders of product footage; empty = the library's
    script: ScriptInput | None = None  # material mode; empty = write a draft from the product sheet
    options: RunOptions = RunOptions()

    @model_validator(mode="after")
    def _needs(self):
        if self.product is None and not self.product_id:
            raise ValueError("give product_id (款名) or the full product sheet")
        if self.product and self.product_id and self.product.name != self.product_id:
            raise ValueError(f"product_id {self.product_id!r} does not match product.name {self.product.name!r}")
        lo, hi = self.options.target_ms
        if lo > hi:
            raise ValueError("options.target_ms must be (min, max)")
        return self

    @property
    def resolved(self) -> bool:
        """True once intake has filled in the product sheet and the footage."""
        return self.product is not None and (self.mode == "material" or bool(self.source_videos))
