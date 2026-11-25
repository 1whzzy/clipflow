"""画面理解 Agent (new): tag frames so later steps know what the viewer actually sees.

Used by `look` (frames from the replay) and by the asset library (product footage). The tags let
code check rules like "the first 3 seconds must show the product" and "a fabric line needs a
fabric picture", and let `match_assets` pick footage of the right color and shot type.
"""
import base64
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from ..contracts import FrameTag, FrameTags, Ms, ShotType
from .base import DATA_NOTICE, Agent


class Frame(BaseModel):
    index: int
    asset_id: str
    at_ms: Ms
    path: str  # a JPEG on disk


class VisionInput(BaseModel):
    product: str
    category: str
    colors: list[str]
    frames: list[Frame]
    reference_paths: list[str] = []  # product shots, to judge "same item"


class TagAnswer(BaseModel):
    index: int
    shot_type: ShotType
    product_visible: Literal["clear", "partial", "none"]
    same_item: Literal["yes", "no", "unsure"]
    color: str | None = None
    action: str | None = None
    note: str = ""


class VisionAnswer(BaseModel):
    """One tag per input frame."""

    tags: list[TagAnswer]


def _image(path: str) -> dict:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}", "detail": "low"}}


class VisionAgent(Agent[VisionInput, VisionAnswer]):
    name = "vision"
    title = "画面理解"
    prompt_id = "vision"
    input_model = VisionInput
    output_model = VisionAnswer
    tier = "vision"

    def messages(self, inp):
        parts: list = [{"type": "text", "text": f"{DATA_NOTICE}\n商品：{inp.product}（{inp.category}），"
                                                f"颜色：{'、'.join(inp.colors)}"}]
        for p in inp.reference_paths:
            if Path(p).exists():
                parts += [{"type": "text", "text": "参考图（商品实拍）："}, _image(p)]
        for f in inp.frames:
            parts.append({"type": "text", "text": f"帧 index={f.index}（{f.at_ms} ms）："})
            parts.append(_image(f.path) if Path(f.path).exists() else {"type": "text", "text": "（图片缺失）"})
        return [SystemMessage(self.prompt.body), HumanMessage(content=parts)]

    def check(self, inp, out):
        want = {f.index for f in inp.frames}
        got = [t.index for t in out.tags]
        problems = []
        if missing := sorted(want - set(got)):
            problems.append(f"缺少这些帧的标注：{missing}")
        if extra := sorted(set(got) - want):
            problems.append(f"这些 index 不存在：{extra}")
        if len(got) != len(set(got)):
            problems.append("同一帧标注了多次")
        return problems

    def repair(self, inp, out):
        """A colour outside the product's colours is a guess: drop it (= not visible) instead of re-asking."""
        tags = [t if not t.color or t.color in inp.colors else t.model_copy(update={"color": None}) for t in out.tags]
        return out.model_copy(update={"tags": tags})


def to_frame_tags(inp: VisionInput, out: VisionAnswer) -> FrameTags:
    frames = {f.index: f for f in inp.frames}
    return FrameTags(product=inp.product, tags=[
        FrameTag(asset_id=frames[t.index].asset_id, at_ms=frames[t.index].at_ms, shot_type=t.shot_type,
                 product_visible=t.product_visible, same_item=t.same_item, color=t.color, action=t.action,
                 note=t.note)
        for t in sorted(out.tags, key=lambda t: t.index)
    ])
