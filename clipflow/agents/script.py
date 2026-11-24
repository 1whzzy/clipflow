"""脚本生成 Agent (V2.0 §5.1), two tasks with one output idea: the words of the video, in order.

- Slice mode (`select_quotes`): pick the anchor's own clauses. The model answers with line numbers
  only; code copies the text from the transcript, so a quote can never be misquoted or invented.
- Material mode (`prepare_script`): split a script into lines. A final script is split, never
  reworded; code checks the lines join back to the original text.
"""
import re
from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import FrameTag, Line, Ms, Product, Quote, QuoteList, ScriptInput, ScriptLine, ScriptLines
from .base import Agent
from .locate import line_text


class QuoteRules(BaseModel):
    target_ms: tuple[Ms, Ms]
    focus: list[str] = []  # the user's chosen selling points: lead with these when the anchor says them
    hook_style: str = "any"
    hook_keywords: list[str] = []
    must_mention: list[str] = []
    opening_connectives: list[str] = []
    hesitations: list[str] = []
    claim_terms: list[str] = []


class SelectInput(BaseModel):
    product: Product
    asset_id: str
    clauses: list[Line]  # word-level clauses inside the located blocks
    tags: list[FrameTag] = []  # what the picture shows around each clause
    rules: QuoteRules
    rework_notes: list[str] = []  # auto_review findings when this is a rework


class PickedQuote(BaseModel):
    seq: int
    line_ids: list[int] = Field(min_length=1, description="consecutive clause numbers")
    role: Literal["opening", "middle", "ending"]
    point: str
    hook_type: str = ""
    reason: str = ""


class SelectAnswer(BaseModel):
    """The clauses to use, in the order they should play."""

    quotes: list[PickedQuote] = Field(min_length=1)


def tag_text(t: FrameTag) -> str:
    """One frame tag as 'time<TAB>shot<TAB>visible<TAB>same item<TAB>color<TAB>action'."""
    return "\t".join([f"{t.at_ms / 1000:.1f}s", t.shot_type, f"商品{t.product_visible}", f"同款{t.same_item}",
                      t.color or "", t.action or ""])


class SelectQuotesAgent(Agent[SelectInput, SelectAnswer]):
    name = "select_quotes"
    title = "脚本生成（切片：挑选主播原话）"
    prompt_id = "select_quotes"
    input_model = SelectInput
    output_model = SelectAnswer
    tier = "creative"
    text_fields = {"clauses": line_text, "tags": tag_text}

    def check(self, inp, out):
        by_i = {c.i: c for c in inp.clauses}
        r, p = inp.rules, inp.product
        problems, used = [], set()
        for q in out.quotes:
            if bad := [i for i in q.line_ids if i not in by_i]:
                problems.append(f"第 {q.seq} 段：行号 {bad} 不存在")
                continue
            if q.line_ids != list(range(q.line_ids[0], q.line_ids[0] + len(q.line_ids))):
                problems.append(f"第 {q.seq} 段：行号必须连续")
            if dup := used & set(q.line_ids):
                problems.append(f"第 {q.seq} 段：行 {sorted(dup)} 已在别的段里用过")
            used |= set(q.line_ids)
            text = quote_text(inp, q)
            for h in r.hesitations:
                if h in text:
                    problems.append(f"第 {q.seq} 段含犹豫/口头禅「{h}」")
            for t in r.claim_terms:
                if t in text and t not in p.verified_claims:
                    problems.append(f"第 {q.seq} 段说了未核实的材质/工艺「{t}」")
        if problems:
            return problems
        first = out.quotes[0]
        if first.role != "opening" or out.quotes[-1].role != "ending":
            problems.append("第一段必须是 opening，最后一段必须是 ending")
        opening = quote_text(inp, first)
        if any(opening.startswith(c) for c in r.opening_connectives):
            problems.append("第一句以连接词开头，单独听不懂")
        if r.hook_keywords and not any(k in opening for k in r.hook_keywords):
            problems.append(f"第一句必须说到 {r.hook_keywords} 之一")
        total = sum(quote_ms(inp, q) for q in out.quotes)
        lo, hi = r.target_ms
        if not lo <= total <= hi:
            problems.append(f"总时长 {total / 1000:.1f} 秒，要求 {lo / 1000:.0f}–{hi / 1000:.0f} 秒")
        return problems


def quote_text(inp: SelectInput, q: PickedQuote) -> str:
    by_i = {c.i: c for c in inp.clauses}
    return "".join(by_i[i].text for i in q.line_ids)


def quote_ms(inp: SelectInput, q: PickedQuote) -> int:
    by_i = {c.i: c for c in inp.clauses}
    return by_i[q.line_ids[-1]].end_ms - by_i[q.line_ids[0]].start_ms


def to_quotes(inp: SelectInput, out: SelectAnswer) -> QuoteList:
    return QuoteList(product=inp.product.name, quotes=[
        Quote(seq=q.seq, asset_id=inp.asset_id, line_ids=q.line_ids, text=quote_text(inp, q), role=q.role,
              point=q.point, hook_type=q.hook_type, reason=q.reason)
        for q in out.quotes
    ])


# ---- material mode --------------------------------------------------------------------------------

class ScriptTaskInput(BaseModel):
    product: Product
    script: ScriptInput
    focus: list[str] = []
    max_line_chars: int = 40
    rework_notes: list[str] = []


class ScriptAnswer(BaseModel):
    """The script as a list of lines (one caption each), in order."""

    lines: list[ScriptLine] = Field(min_length=1)


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:“”\"'…]", "", text)


class PrepareScriptAgent(Agent[ScriptTaskInput, ScriptAnswer]):
    name = "prepare_script"
    title = "脚本生成（素材带货：整理口播脚本）"
    prompt_id = "prepare_script"
    input_model = ScriptTaskInput
    output_model = ScriptAnswer
    tier = "creative"

    def check(self, inp, out):
        problems = []
        if inp.script.status == "final" and _norm("".join(l.text for l in out.lines)) != _norm(inp.script.text):
            problems.append("定稿脚本只能拆句，不能增删改字；拼回去必须和原文一致")
        if long := [l.seq for l in out.lines if len(l.text) > inp.max_line_chars]:
            problems.append(f"这些句子超过 {inp.max_line_chars} 字，请再拆开：{long}")
        if out.lines[0].role != "opening" or out.lines[-1].role != "ending":
            problems.append("第一句必须是 opening，最后一句必须是 ending")
        return problems


def to_script_lines(inp: ScriptTaskInput, out: ScriptAnswer) -> ScriptLines:
    return ScriptLines(product=inp.product.name, script_version=inp.script.version, lines=out.lines)
