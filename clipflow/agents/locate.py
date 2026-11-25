"""商品定位 Agent (new in the finished-video design): find where a replay sells THIS product.

The anchor rarely says the product name, so the model works from price, colors, cut and selling
points. It answers with line numbers and exact quotes; code converts lines to milliseconds and
checks every quote really is in the transcript, so a made-up block can't get through.
"""
from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import Block, Line, Product, ProductBlocks, TimeRange
from .base import Agent


class LocateInput(BaseModel):
    product: Product
    asset_id: str
    window: TimeRange | None = None  # set when the user gave a rough time range
    lines: list[Line]


class FoundBlock(BaseModel):
    first_line: int
    last_line: int
    confidence: Literal["high", "medium", "low"]
    evidence: list[str] = Field(min_length=1, description="1-3 exact quotes from these lines that identify the product")
    link_no: int | None = None


class LocateAnswer(BaseModel):
    """Stretches of the transcript where the anchor sells the product. Empty list if it is not sold here."""

    blocks: list[FoundBlock]


def _clock(ms: int) -> str:
    s = ms / 1000
    if s >= 3600:
        return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:04.1f}"
    return f"{int(s // 60)}:{s % 60:04.1f}"


def line_text(line: Line) -> str:
    """One transcript line as 'number<TAB>[start-end]<TAB>text'."""
    return f"{line.i}\t[{_clock(line.start_ms)}-{_clock(line.end_ms)}]\t{line.text}"


class LocateAgent(Agent[LocateInput, LocateAnswer]):
    name = "locate"
    title = "商品定位"
    prompt_id = "locate"
    input_model = LocateInput
    output_model = LocateAnswer
    tier = "reasoning"
    text_fields = {"lines": line_text}

    def check(self, inp, out):
        by_i = {line.i: line for line in inp.lines}
        problems = []
        spans = []
        for n, b in enumerate(out.blocks, 1):
            if b.first_line not in by_i or b.last_line not in by_i:
                problems.append(f"第 {n} 段：行号 {b.first_line}–{b.last_line} 不在输入里")
                continue
            if b.last_line < b.first_line:
                problems.append(f"第 {n} 段：last_line 小于 first_line")
                continue
            text = "".join(by_i[i].text for i in sorted(by_i) if b.first_line <= i <= b.last_line)
            for q in b.evidence:
                if q not in text:
                    problems.append(f"第 {n} 段：证据「{q[:30]}」不是这些行里的原话")
            spans.append((b.first_line, b.last_line, n))
        spans.sort()
        for (_a1, b1, n1), (a2, _, n2) in zip(spans, spans[1:], strict=False):  # neighbours
            if a2 <= b1:
                problems.append(f"第 {n1} 段和第 {n2} 段重叠")
        return problems


def to_blocks(inp: LocateInput, out: LocateAnswer) -> ProductBlocks:
    by_i = {line.i: line for line in inp.lines}
    return ProductBlocks(asset_id=inp.asset_id, product=inp.product.name, blocks=[
        Block(start_ms=by_i[b.first_line].start_ms, end_ms=by_i[b.last_line].end_ms, confidence=b.confidence,
              evidence=b.evidence, line_ids=[i for i in sorted(by_i) if b.first_line <= i <= b.last_line],
              link_no=b.link_no)
        for b in out.blocks
    ])
