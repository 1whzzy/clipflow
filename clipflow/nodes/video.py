"""Nodes 14–18: the shared video stage (Plan → Timeline → MP4 → checks → human review → delivery)."""
from langgraph.runtime import Runtime

from ..contracts import (
    AudioItem, Caption, Decision, Delivery, EndCard, Plan, Product, QCCheck, QCReport, RenderResult, RunRequest,
    Timeline, VideoItem,
)
from ..graph.context import RunContext
from ..graph.state import RunState
from ._human import ask, blocking_issues, waivable_findings
from ._util import load, request
from .plan import apply_edits

END_CARD_MS = 3000
MISSING = "placeholder:missing"


def split_caption(text: str, duration_ms: int, max_chars: int) -> list[tuple[str, int, int]]:
    """One spoken line → on-screen captions of at most `max_chars`, split at punctuation where
    possible, each shown for a share of the shot proportional to its length."""
    import re

    pieces = [p for p in re.split(r"(?<=[，,。！？!?；;])", text) if p.strip("，,。 ")]
    cues: list[str] = []
    for p in pieces or [text]:
        p = p.strip().rstrip("，,。")
        while len(p) > max_chars:
            cues.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            if cues and len(cues[-1]) + len(p) <= max_chars // 2:  # glue very short bits to the previous cue
                cues[-1] += p
            else:
                cues.append(p)
    total = sum(len(c) for c in cues) or 1
    out, at = [], 0
    for n, c in enumerate(cues):
        end = duration_ms if n == len(cues) - 1 else at + duration_ms * len(c) // total
        out.append((c, at, end))
        at = end
    return out


def make_timeline(plan: Plan, req: RunRequest, version: int, cue_chars: int = 16) -> Timeline:
    """Pure function: same Plan + settings → same Timeline. Only code writes timelines.
    Each shot brings its own picture and its own speech, so edits can't misalign them."""
    video, audio, captions, at = [], [], [], 0
    for shot in plan.shots:
        clip = shot.clip
        video.append(VideoItem(shot_seq=shot.seq, asset_id=clip.asset_id if clip else MISSING,
                               in_ms=clip.in_ms if clip else 0, out_ms=(clip.in_ms if clip else 0) + shot.duration_ms,
                               at_ms=at))
        if shot.speech:  # live: the anchor, even under a product shot; voice: TTS (material mode)
            s = shot.speech
            audio.append(AudioItem(kind=s.kind, asset_id=s.asset_id, in_ms=s.in_ms,
                                   out_ms=min(s.out_ms, s.in_ms + shot.duration_ms), at_ms=at))
        position = "top" if shot.shot_type in ("look_full", "detail_fit") else "bottom"
        captions += [Caption(start_ms=at + a, end_ms=at + b, text=t, position=position)
                     for t, a, b in split_caption(shot.caption, shot.duration_ms, cue_chars)]
        at += shot.duration_ms
    return Timeline(run_id=plan.run_id, plan_version=plan.version, version=version, video=video, audio=audio,
                    captions=captions, end_card=EndCard(start_ms=at, end_ms=at + END_CARD_MS,
                                                        lines=end_card_lines(req.product, req.options.cta)))


def end_card_lines(product: Product, cta: str) -> list[str]:
    lines = [product.name, " · ".join(product.colors)]
    if product.price is not None and product.price_verified:  # never show an unconfirmed price
        lines.append(f"¥{product.price:g}")
    if cta:
        lines.append(cta)
    return [line for line in lines if line]


def build_timeline(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """14. Plan → Timeline (tracks, in/out points, captions, end card). Code only."""
    ctx = runtime.context
    req = request(ctx, state)
    plan = load(ctx, state, "plan", Plan)
    version = load(ctx, state, "timeline", Timeline).version + 1 if state.get("timeline") else 1
    return {"timeline": ctx.store.put("timeline", make_timeline(plan, req, version, ctx.rules.caption_cue_chars))}


def render(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """15. Timeline → MP4 (ffmpeg worker). Re-renders reuse unchanged shots."""
    ctx = runtime.context
    timeline = load(ctx, state, "timeline", Timeline)
    previous = load(ctx, state, "render", RenderResult) if state.get("render") else None
    return {"render": ctx.store.put("render", ctx.renderer.render(timeline, previous))}


def qc(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """16. Measure the MP4 against the output spec (1080x1920, 30 fps, −14 LUFS, length on target).

    Every failed check says who can fix it (system / human / admin); routing.after_qc acts on that.
    A technical failure that survives the automatic retries ends the run as `failed`."""
    ctx = runtime.context
    req = request(ctx, state)
    timeline = load(ctx, state, "timeline", Timeline)
    result = load(ctx, state, "render", RenderResult)
    m = ctx.renderer.measure(result.path)
    c = timeline.canvas
    checks = []
    if (m.width, m.height) != (c.width, c.height):
        checks.append(QCCheck(id="qc.size", severity="fail", detail=f"{m.width}x{m.height}", fix_by="system"))
    if abs(m.fps - c.fps) > 0.01:
        checks.append(QCCheck(id="qc.fps", severity="fail", detail=f"{m.fps} fps", fix_by="system"))
    if m.lufs is not None and abs(m.lufs - c.lufs) > 1.0:
        checks.append(QCCheck(id="qc.loudness", severity="fail", detail=f"{m.lufs} LUFS", fix_by="system"))
    if abs(m.duration_ms - timeline.duration_ms) > 200:
        checks.append(QCCheck(id="qc.sync", severity="fail", detail="rendered length differs from the timeline"))
    missing = [v.shot_seq for v in timeline.video if v.asset_id == MISSING]
    if missing:
        checks.append(QCCheck(id="qc.missing_footage", severity="fail", fix_by="human",
                              detail=f"镜头 {missing} 没有画面，请选素材或删除"))
    lo, hi = req.options.target_ms
    if not lo <= m.duration_ms - END_CARD_MS <= hi:
        checks.append(QCCheck(id="qc.duration", severity="warn", detail=f"{m.duration_ms / 1000:.1f} s"))
    report = QCReport(run_id=req.run_id, timeline_version=timeline.version, measured=m, checks=checks,
                      passed=not any(x.severity == "fail" for x in checks))
    out = {"qc": ctx.store.put("qc", report)}
    if not report.passed:
        out["qc_round"] = state.get("qc_round", 0) + 1
        system_left = out["qc_round"] <= ctx.limits.qc_rounds
        stuck = [x for x in checks if x.severity == "fail"
                 and (x.fix_by == "admin" or (x.fix_by == "system" and not system_left))]
        if stuck:
            out["status"] = "failed"
            out["flags"] = [f"qc_failed:{x.id}:{x.detail}" for x in stuck]
    return out


def review_video(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """17. The mandatory human review. Approval is impossible while a must-fix finding or a failed
    QC check is open; waiving other findings needs a reason. Edits patch the Plan; text edits are
    re-checked by auto_review before re-rendering. At most `limits.video_rounds` edit rounds."""
    ctx = runtime.context
    plan = load(ctx, state, "plan", Plan)
    timeline = load(ctx, state, "timeline", Timeline)
    rounds_left = ctx.limits.video_rounds - state.get("video_round", 0)
    decision = ask("video", {"video": load(ctx, state, "render", RenderResult).path, "qc": state.get("qc"),
                             "flags": state.get("flags", []), "rounds_left": rounds_left},
                   version=timeline.version, plan=plan, blocking=blocking_issues(ctx, state, plan, "video"),
                   waivable=waivable_findings(ctx, state, plan), allow_edit=rounds_left > 0)
    out = {"video_decision": ctx.store.put("decision", decision)}
    if decision.verdict == "reject":
        out["status"] = "rejected"
    elif decision.verdict == "edit":
        out["plan"] = ctx.store.put("plan", apply_edits(plan, decision))
        out["plan_by"] = "human"
        out["video_round"] = state.get("video_round", 0) + 1
        out["phase"] = "video"
        out["review_round"] = 0
    return out


def deliver(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """18. Package the approved video. Re-checks the gate as a last line of defence: a video with an
    open must-fix problem is never delivered. Publishing stays manual (out of scope)."""
    ctx = runtime.context
    req = request(ctx, state)
    plan = load(ctx, state, "plan", Plan)
    if blocking := blocking_issues(ctx, state, plan, "video"):
        return {"status": "stopped", "flags": [f"delivery_blocked:{b}" for b in blocking]}
    decision = load(ctx, state, "video_decision", Decision)
    result = load(ctx, state, "render", RenderResult)
    timeline = load(ctx, state, "timeline", Timeline)
    report = load(ctx, state, "qc", QCReport)
    waived = [f"{w}（理由：{decision.reason}）" for w in waivable_findings(ctx, state, plan)]
    delivery = Delivery(run_id=req.run_id, video_path=result.path, plan_version=timeline.plan_version,
                        timeline_version=timeline.version, qc_passed=report.passed, waived=waived)
    return {"delivery": ctx.store.put("delivery", delivery), "status": "done"}
