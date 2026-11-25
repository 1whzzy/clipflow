"""The rule library: editing and compliance rules checked by code, not by prompts.

Rules come from config.yaml (`rules`, `selling_point_priority`), which collects the editor SOP,
the V1.0 workflow's review checklist and the review of the first pilot video. Checking them in code makes them
explainable and testable; the compliance agent only handles what rules can't (exaggeration,
misleading comparisons).
"""
import re
from dataclasses import dataclass, field

from .contracts import Finding, Plan, Product
from .settings import config


# Findings a reviewer may NOT waive: they are fact and compliance errors, and the pilot's target
# for them in delivered videos is zero (spec 2.2). Everything else (structure, variety, the
# compliance agent's style findings) a reviewer may accept, with a reason.
MUST_FIX = {
    "rule.banned_word", "rule.price_unverified", "rule.price_mismatch", "rule.unverified_claim",
    "rule.caption_edit",
}


@dataclass(frozen=True)
class Rules:
    opening_connectives: list[str] = field(default_factory=list)
    hesitations: list[str] = field(default_factory=list)
    deictic: list[str] = field(default_factory=list)
    claim_terms: list[str] = field(default_factory=list)
    protected_terms: list[str] = field(default_factory=list)
    banned_words: list[str] = field(default_factory=list)
    max_support_points: int = 2
    max_same_shot_type_in_a_row: int = 2
    caption_cue_chars: int = 16
    selling_point_priority: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Rules:
        cfg = config()
        r = cfg.get("rules", {})
        return cls(**{k: r[k] for k in cls.__dataclass_fields__ if k in r},
                   selling_point_priority=cfg.get("selling_point_priority", {}))

    def priority_for(self, product: Product) -> tuple[str, list[str]]:
        """(rule id, order). Falls back to 其他 for categories the team hasn't written a rule for."""
        band = "200以下" if (product.price or 0) < 200 else "200以上"
        for key in (f"{product.category}_{band}", product.category):
            if key in self.selling_point_priority:
                return f"rule:priority.{key}", self.selling_point_priority[key]
        return "rule:priority.其他", self.selling_point_priority.get("其他", [])

    def unverified_claims(self, text: str, product: Product) -> list[str]:
        return [t for t in self.claim_terms if t in text and t not in product.verified_claims]


# "169元", "169块", "价格…来169", "只要99": a number said as a price, with or without a unit.
PRICE = re.compile(r"(\d{2,5})\s*(?:元|块|米)|(?:价格|到手|只要|仅需|开到|来)\D{0,6}?(\d{2,5})")


def plan_rule_findings(plan: Plan, product: Product, rules: Rules) -> list[Finding]:
    """Deterministic checks over a plan. `block` findings stop the run; `fix` findings send it back."""
    out: list[Finding] = []
    for shot in plan.shots:
        text = f"{shot.line} {shot.caption}"
        for w in rules.banned_words:
            if w in text:
                out.append(Finding(rule_id="rule.banned_word", severity="block", seq=shot.seq,
                                   detail=f"极限词「{w}」", fix_at="content"))
        for t in rules.unverified_claims(text, product):
            out.append(Finding(rule_id="rule.unverified_claim", severity="fix", seq=shot.seq,
                               detail=f"「{t}」不在商品资料 verified_claims 里", fix_at="content"))
        for m in PRICE.finditer(text):
            said = float(m.group(1) or m.group(2))
            if product.price is None or said != float(product.price):
                out.append(Finding(rule_id="rule.price_mismatch", severity="fix", seq=shot.seq,
                                   detail=f"价格「{m.group(0)}」与商品资料（{product.price}）不符", fix_at="content"))
            elif not product.price_verified:
                out.append(Finding(rule_id="rule.price_unverified", severity="fix", seq=shot.seq,
                                   detail="价格尚未核实（price_verified=false），不能出现在成片里", fix_at="content"))
        for h in rules.hesitations:
            if h in shot.line:
                out.append(Finding(rule_id="rule.hesitation", severity="fix", seq=shot.seq,
                                   detail=f"含犹豫/口头禅「{h}」", fix_at="content"))
    first = plan.shots[0]
    if first.purpose != "hook":
        out.append(Finding(rule_id="rule.hook_first", severity="fix", seq=first.seq, detail="首镜必须是钩子"))
    if any(first.line.startswith(c) for c in rules.opening_connectives):
        out.append(Finding(rule_id="rule.opening_connective", severity="fix", seq=first.seq,
                           detail="第一句以连接词开头，单独听不懂", fix_at="content"))
    run, prev = 0, None
    for shot in plan.shots:
        run = run + 1 if shot.shot_type == prev else 1
        prev = shot.shot_type
        if run > rules.max_same_shot_type_in_a_row:
            out.append(Finding(rule_id="rule.shot_variety", severity="fix", seq=shot.seq,
                               detail=f"连续 {run} 个 {shot.shot_type} 画面"))
    for shot in plan.shots:
        if shot.clip is None:
            out.append(Finding(rule_id="rule.missing_footage", severity="note", seq=shot.seq,
                               detail="没有匹配到素材，进入补拍清单", fix_at="assets"))
    unique = {(f.rule_id, f.seq, f.detail): f for f in out}  # line and caption often say the same thing
    return list(unique.values())


def caption_edit_problems(new_text: str, allowed_words: set[str], product: Product, rules: Rules) -> list[str]:
    """A caption fix may only use product words or words already said in the transcript
    (this is what would have blocked the model's guess 一体织)."""
    problems = [f"「{t}」未经核实" for t in rules.unverified_claims(new_text, product)]
    problems += [f"「{t}」不在商品资料或转写稿里" for t in rules.protected_terms + rules.claim_terms
                 if t in new_text and t not in allowed_words]
    return problems
