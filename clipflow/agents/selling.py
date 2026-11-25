"""卖点匹配 Agent (V2.0 §5.2): which selling points, in which order, and why.

Order by evidence, not by feel: every point must cite a category rule or a top-video case. SOP
2.2.2: one main point, at most two supporting ones. Priorities are renumbered locally instead of
re-asking the model (V2.0: "由本地修复逻辑重排，而不是重问模型").
"""
from pydantic import BaseModel

from ..contracts import Product, SellingPoints
from .base import Agent


class ContentItem(BaseModel):
    seq: int
    text: str


class Case(BaseModel):
    case_id: str
    summary: str
    hook_type: str = ""


class SellingInput(BaseModel):
    product: Product
    content: list[ContentItem]  # quotes (slice) or script lines (material)
    rule_id: str
    priority_rule: list[str]  # from config.yaml selling_point_priority
    cases: list[Case] = []  # similar top videos (爆款拆解素材库), when available
    focus: list[str] = []  # the user's chosen points; one of them should be the main point if the content has it
    max_support: int = 2


class SellingAgent(Agent[SellingInput, SellingPoints]):
    name = "selling"
    title = "卖点匹配"
    prompt_id = "selling"
    input_model = SellingInput
    output_model = SellingPoints
    tier = "reasoning"

    def repair(self, inp, out):
        """Renumber priorities 1..n, and reduce evidence like 「rule:priority.其他：显瘦属于核心属性…」 to the id
        it starts with (a real run lost an attempt to that). Unknown ids are left for `check` to reject."""
        known = sorted({c.case_id for c in inp.cases} | {inp.rule_id} | {f"user:{f}" for f in inp.focus},
                       key=len, reverse=True)

        def to_id(e: str) -> str:
            return next((k for k in known if e != k and e.startswith(k) and e[len(k):len(k) + 1] in "：: （(-—,，"), e)

        items = sorted(out.items, key=lambda x: x.priority)
        return out.model_copy(update={"items": [
            i.model_copy(update={"priority": n, "evidence": list(dict.fromkeys(to_id(e) for e in i.evidence))})
            for n, i in enumerate(items, 1)]})

    def check(self, inp, out):
        """Integrity: every point must cite a real rule or case, and refer to content that exists."""
        problems = []
        known = {c.case_id for c in inp.cases} | {inp.rule_id} | {f"user:{f}" for f in inp.focus}
        seqs = {c.seq for c in inp.content}
        for i in out.items:
            if bad := [e for e in i.evidence if e not in known]:
                problems.append(f"「{i.point}」引用了不存在的依据 {bad}，只能用 {sorted(known)}")
            if bad := [s for s in i.content_refs if s not in seqs]:
                problems.append(f"「{i.point}」对应的内容序号 {bad} 不存在")
        return problems

    def quality(self, inp, out):
        problems = []
        mains = [i for i in out.items if i.weight == "main"]
        if len(mains) != 1:
            problems.append(f"必须正好 1 个主推卖点，现在是 {len(mains)} 个")
        if (n := len(out.items) - len(mains)) > inp.max_support:
            problems.append(f"辅助卖点最多 {inp.max_support} 个，现在是 {n} 个")
        if mains and out.items[0].weight != "main":
            problems.append("主推卖点必须排第一")
        said = "".join(c.text for c in inp.content)
        wanted = [f for f in inp.focus if f in said]
        if wanted and mains and not any(f in mains[0].point or mains[0].point in f for f in wanted):
            problems.append(f"用户指定重点 {wanted} 在内容里有讲到，主推卖点应从中选")
        return problems
