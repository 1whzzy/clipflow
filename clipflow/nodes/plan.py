"""Nodes 9–12: the shared plan stage (words → Plan → checked → confirmed)."""
from langgraph.runtime import Runtime

from ..agents import AgentFailed, AssetAgent, ComplianceAgent, SellingAgent, StoryboardAgent
from ..agents.assets import AssetInput, Candidate, to_clips
from ..agents.compliance import ComplianceInput, ReviewShot, verdict
from ..agents.selling import Case, ContentItem, SellingInput
from ..agents.storyboard import StoryboardInput, StoryItem, to_plan
from ..agents.script import _norm
from ..contracts import ClipRef, Decision, Finding, FrameTags, Plan, QuoteList, Review, ScriptLines, Speech, Transcript
from ..graph.context import RunContext
from ..graph.state import RunState
from ..rules import MUST_FIX, caption_edit_problems, plan_rule_findings
from ._human import ask, blocking_issues, waivable_findings
from ._util import load, request, rework_notes, trace

CHARS_PER_MINUTE = 220  # V2.0 §5.1: script length → duration estimate, until TTS gives real durations


def _items(ctx: RunContext, state: RunState) -> list[StoryItem]:
    if state["mode"] == "slice":
        clauses = {c.i: c for c in load(ctx, state, "clauses", Transcript).lines}
        tags = load(ctx, state, "frame_tags", FrameTags).tags if state.get("frame_tags") else []
        items = []
        for q in load(ctx, state, "quotes", QuoteList).quotes:
            start, end = clauses[q.line_ids[0]].start_ms, clauses[q.line_ids[-1]].end_ms
            seen = [t for t in tags if start <= t.at_ms <= end]
            items.append(StoryItem(seq=q.seq, text=q.text, duration_ms=end - start, point=q.point,
                                   source_asset_id=q.asset_id, source_line_ids=q.line_ids,
                                   live_shot_type=seen[0].shot_type if seen else None))
        return items
    lines = load(ctx, state, "script_lines", ScriptLines).lines
    return [StoryItem(seq=l.seq, text=l.text, point=l.point,
                      duration_ms=max(1000, len(_norm(l.text)) * 60_000 // CHARS_PER_MINUTE)) for l in lines]


def storyboard(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """9. 卖点匹配 Agent, then 分镜生成 Agent → a new Plan version (whole, never merged with the old one)."""
    ctx = runtime.context
    req = request(ctx, state)
    items = _items(ctx, state)
    rule_id, order = ctx.rules.priority_for(req.product)
    o = req.options
    cases = [Case(**c) for c in ctx.cases.similar(req.product.category, req.product.price, o.focus, o.hook_style)]
    s_inp = SellingInput(product=req.product, content=[ContentItem(seq=i.seq, text=i.text) for i in items],
                         rule_id=rule_id, priority_rule=order, cases=cases, focus=o.focus,
                         max_support=ctx.rules.max_support_points)
    selling = SellingAgent().run_cached(s_inp, ctx.models, ctx.store)

    previous = load(ctx, state, "plan", Plan).version if state.get("plan") else 0
    b_inp = StoryboardInput(run_id=req.run_id, mode=req.mode, product=req.product.name, category=req.product.category,
                            items=items, selling=selling.output, target_ms=req.options.target_ms,
                            max_same_in_a_row=ctx.rules.max_same_shot_type_in_a_row, plan_version=previous + 1,
                            rework_notes=rework_notes(ctx, state))
    board = StoryboardAgent().run_cached(b_inp, ctx.models, ctx.store)
    return {"selling": ctx.store.put("selling", selling.output),
            "plan": ctx.store.put("plan", to_plan(b_inp, board.output)), "plan_by": "agent",
            "trace": [trace("storyboard", selling), trace("storyboard", board)]}


def match_assets(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """10. Footage for every shot. Slice: the live picture by default, a product shot over it when the
    live picture doesn't show what is being said. Material: always from the asset library.
    No candidates → the shot goes on the reshoot list; a failed ranking takes the first candidate."""
    ctx = runtime.context
    req = request(ctx, state)
    plan = load(ctx, state, "plan", Plan)
    tags = load(ctx, state, "frame_tags", FrameTags).tags if state.get("frame_tags") else []
    clauses = {c.i: c for c in load(ctx, state, "clauses", Transcript).lines} if state["mode"] == "slice" else {}
    shots, gaps, entries, flags = [], [], [], []
    ranking: dict[int, tuple[AssetInput, Speech | None]] = {}  # position in `shots` → agent input
    for shot in plan.shots:
        live = None
        if shot.source_line_ids and clauses:
            start, end = clauses[shot.source_line_ids[0]].start_ms, clauses[shot.source_line_ids[-1]].end_ms
            live = Speech(kind="live", asset_id=shot.source_asset_id, in_ms=start, out_ms=end)
            seen = [t for t in tags if start <= t.at_ms <= end]
            shows_product = bool(seen) and all(t.product_visible != "none" for t in seen)
            needs_detail = shot.shot_type.startswith("detail") and all(t.shot_type != shot.shot_type for t in seen)
            if shows_product and not needs_detail:  # the live picture already shows what is said
                shots.append(shot.model_copy(update={"clip": _picture(live), "speech": live}))
                continue
        found = ctx.library.find(req.product.name, req.product.colors, shot.shot_type, shot.duration_ms)
        if not found:
            shots.append(shot.model_copy(update={"clip": _picture(live), "speech": live or shot.speech}))
            if live is None:
                gaps.append(f"镜头 {shot.seq}：缺 {shot.shot_type} 素材（{shot.duration_ms / 1000:.1f} 秒）")
            continue
        ranking[len(shots)] = (AssetInput(
            seq=shot.seq, line=shot.line, purpose=shot.purpose, shot_type=shot.shot_type, need_ms=shot.duration_ms,
            colors=req.product.colors, candidates=[
                Candidate(index=n, asset_id=a, in_ms=r.start_ms, out_ms=r.end_ms, shot_type=r.shot_type, color=r.color)
                for n, (a, r) in enumerate(found[:8])]), live)
        shots.append(shot)  # placeholder, filled after the ranking below
    # rank all shots that need product footage at once
    inputs = [inp for inp, _ in ranking.values()]
    results = AssetAgent().run_many(inputs, ctx.models, ctx.store, ctx.limits.model_concurrency)
    for (pos, (inp, live)), result in zip(ranking.items(), results):
        if isinstance(result, AgentFailed):
            clip, alternates = to_clips(inp, AssetAgent.output_model(choice=0))
            flags.append(f"assets_degraded:shot{inp.seq}:{result}")
        else:
            clip, alternates = to_clips(inp, result.output)
            entries.append(trace("match_assets", result))
        shot = shots[pos]
        shots[pos] = shot.model_copy(update={"clip": clip, "alternates": alternates, "speech": live or shot.speech})
    new = plan.model_copy(update={"shots": shots, "gaps": gaps})
    return {"plan": ctx.store.put("plan", new), "trace": entries, "flags": flags}


def auto_review(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """11. Rules in code first, then 合规审核 Agent (only judges). The verdict is computed in code."""
    ctx = runtime.context
    req = request(ctx, state)
    plan = load(ctx, state, "plan", Plan)
    found = plan_rule_findings(plan, req.product, ctx.rules)
    if state.get("phase") == "video" and state.get("clauses"):  # a reviewer changed captions
        allowed = {w for line in load(ctx, state, "clauses", Transcript).lines for w in ctx.rules.protected_terms
                   + ctx.rules.claim_terms if w in line.text} | set(req.product.selling_points) | set(req.product.colors)
        for s in plan.shots:
            if s.caption != s.line:
                for p in caption_edit_problems(s.caption, allowed, req.product, ctx.rules):
                    found.append(_finding("rule.caption_edit", s.seq, p))
    inp = ComplianceInput(product=req.product, plan_version=plan.version, rule_findings=found,
                          shots=[ReviewShot(seq=s.seq, purpose=s.purpose, line=s.line, caption=s.caption)
                                 for s in plan.shots])
    entries, flags = [], []
    try:
        result = ComplianceAgent().run_cached(inp, ctx.models, ctx.store)
        answer = result.output
        entries.append(trace("auto_review", result))
    except AgentFailed as e:
        answer = None
        flags.append(f"compliance_model_unavailable:{e}")
    review = verdict(plan.version, found, answer)
    review_id = ctx.store.put("review", review)
    out = {"review": review_id, "trace": entries, "flags": flags}
    if review.verdict == "hard_fail":
        out["status"] = "stopped"
        return out
    if state.get("plan_by") == "human":  # a reviewer's plan goes back to the reviewer as is
        out["best_plan"] = None
        return out

    # Remember the best agent-made version: reworks don't always improve the plan (on the real
    # pilot runs the first rework helped, the second made it worse), so the reviewer gets the
    # best one seen, not simply the last one.
    score = _score(review)
    best = state.get("best_plan")
    if best is None or score < tuple(best["score"]):
        best = {"plan": state["plan"], "review": review_id, "score": list(score)}
        out["best_plan"] = best
    if review.verdict == "revise":
        out["review_round"] = state.get("review_round", 0) + 1
        if out["review_round"] > ctx.limits.review_rounds:  # reworks used up: hand over the best version
            # (a discarded later version was never shown to anyone, so no decision can refer to it)
            out["flags"] = flags + [f"review_unresolved:plan_v{ctx.store.get(best['plan'], Plan).version}"]
            out["plan"], out["review"] = best["plan"], best["review"]
    return out


def _score(review: Review) -> tuple[int, int]:
    """Lower is better: must-fix problems first, then other problems to fix."""
    fixes = [f for f in review.findings if f.severity in ("block", "fix")]
    return (sum(f.rule_id in MUST_FIX for f in fixes), len(fixes))


def _finding(rule_id: str, seq: int, detail: str) -> Finding:
    return Finding(rule_id=rule_id, severity="fix", seq=seq, detail=detail, fix_at="storyboard")


def _picture(live: Speech | None) -> ClipRef | None:
    return ClipRef(asset_id=live.asset_id, in_ms=live.in_ms, out_ms=live.out_ms) if live else None


def confirm_plan(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """12. Pause for a human to approve, edit or reject the Plan (can be turned off per run).

    The decision must name the plan version it is about; approving is impossible while a must-fix
    finding is open, and waiving the other findings needs a reason (see _human.py).
    Nothing before the pause has side effects, so re-running this node on resume is safe."""
    ctx = runtime.context
    req = request(ctx, state)
    plan = load(ctx, state, "plan", Plan)
    if not req.options.confirm_plan:
        return {"plan_decision": None}
    decision = ask("plan", {"plan": plan.model_dump(mode="json"), "review": state.get("review"),
                            "flags": state.get("flags", [])},
                   version=plan.version, plan=plan, blocking=blocking_issues(ctx, state, plan, "plan"),
                   waivable=waivable_findings(ctx, state, plan))
    out = {"plan_decision": ctx.store.put("decision", decision)}
    if decision.verdict == "reject":
        out["status"] = "rejected"
    elif decision.verdict == "edit":
        out["plan"] = ctx.store.put("plan", apply_edits(plan, decision))
        out["plan_by"] = "human"
        out["review_round"] = 0
    return out


def apply_edits(plan: Plan, decision: Decision) -> Plan:
    """Patch the Plan with a reviewer's edits → a new Plan version. Only the named shots change;
    each shot keeps its own speech, so deleting or reordering never shifts the audio.
    Edits were validated against this plan by _human.ask."""
    shots = {s.seq: s for s in plan.shots}
    order = [s.seq for s in plan.shots]
    for e in decision.edits:
        shot = shots.get(e.seq) if e.seq is not None else None
        if e.kind == "caption":
            shots[e.seq] = shot.model_copy(update={"caption": e.value})
        elif e.kind == "asset":
            clip = shot.alternates[e.value] if isinstance(e.value, int) else ClipRef.model_validate(e.value)
            shots[e.seq] = shot.model_copy(update={"clip": clip})
        elif e.kind == "cut" and shot.clip:
            clip = shot.clip.model_copy(update={"in_ms": e.value["in_ms"], "out_ms": e.value["out_ms"]})
            speech = shot.speech
            if speech and speech.kind == "live" and speech.asset_id == clip.asset_id:
                speech = speech.model_copy(update={"in_ms": clip.in_ms, "out_ms": clip.out_ms})
            shots[e.seq] = shot.model_copy(update={"clip": clip, "speech": speech,
                                                   "duration_ms": clip.out_ms - clip.in_ms})
        elif e.kind == "delete":
            order.remove(e.seq)
        elif e.kind == "order":
            order = list(e.value)
        # music / cover are render settings: they live on the Timeline, not on a shot
    return plan.model_copy(update={"version": plan.version + 1, "shots": [shots[s] for s in order]})
