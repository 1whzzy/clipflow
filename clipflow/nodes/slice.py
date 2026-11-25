"""Nodes 3–7: the slice-mode front (replay → the anchor's best lines). Run as the `slice_front` subgraph."""
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from ..agents import AgentFailed, LocateAgent, SelectQuotesAgent, VisionAgent
from ..agents.locate import LocateInput, to_blocks
from ..agents.script import QuoteRules, SelectInput, to_quotes
from ..agents.vision import VisionInput, to_frame_tags
from ..contracts import Block, FrameTags, ProductBlocks, TimeRange, Transcript
from ..contracts.request import parse_ms
from ..graph.context import RunContext
from ..graph.state import RunState
from ._util import load, request, rework_notes, trace, warning_flags


def transcribe(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """3. Replay → timestamped transcript (GPU worker, cached by file + range)."""
    ctx = runtime.context
    src = request(ctx, state).source_videos[0]  # one replay per run in the pilot; several = one block search each
    window = TimeRange(start_ms=src.start_ms, end_ms=src.end_ms) if src.start_ms is not None and src.end_ms else None
    return {"transcript": ctx.store.put("transcript", ctx.asr.transcribe(src.asset_id, window))}


def locate_product(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """4. 商品定位 Agent. Not found → pause and ask the user for a rough time range.

    The agent call comes before the pause, which is safe because it is cached: on resume the
    node re-runs from the top and gets the same answer without a model call."""
    ctx = runtime.context
    req = request(ctx, state)
    transcript = load(ctx, state, "transcript", Transcript)
    linked = ctx.library.linked_blocks(req.product.name, transcript.asset_id)
    if linked and linked.blocks:  # ingestion already attributed this replay and a person confirmed it
        return {"blocks": ctx.store.put("blocks", linked), "status": "running", "flags": ["blocks_from_ingestion"]}
    hint = state.get("time_hint")
    window = TimeRange(**hint) if hint else None
    lines = [ln for ln in transcript.lines
             if window is None or (ln.end_ms > window.start_ms and ln.start_ms < window.end_ms)]
    entries, flags, found = [], [], []
    # an hour of replay is ~1,800 lines: ask in chunks (each cached on its own), then merge
    inputs = [LocateInput(product=req.product, asset_id=transcript.asset_id, window=window, lines=chunk)
              for chunk in chunks(lines, ctx.limits.locate_lines_per_call, ctx.limits.locate_overlap_lines)]
    results = LocateAgent().run_many(inputs, ctx.models, ctx.store, ctx.limits.model_concurrency)
    for inp, result in zip(inputs, results, strict=True):
        if isinstance(result, AgentFailed):
            flags.append(f"locate_failed:lines{inp.lines[0].i}-{inp.lines[-1].i}:{result}")
            continue
        found += [b for b in to_blocks(inp, result.output).blocks if b.confidence != "low"]
        entries.append(trace("locate_product", result))
    blocks = ProductBlocks(asset_id=transcript.asset_id, product=req.product.name, blocks=_merge(found))
    if blocks.blocks:
        return {"blocks": ctx.store.put("blocks", blocks), "status": "running", "trace": entries, "flags": flags}

    answer = interrupt({"kind": "needs_input", "node": "locate_product",
                        "question": f"在回放里没找到「{req.product.name}」的讲解，请给出大致时段，或取消。",
                        "expects": {"start": "如 1:02:30 或毫秒", "end": "如 1:10:00 或毫秒"},
                        "or": {"action": "cancel"}})
    if answer.get("action") == "cancel":
        return {"blocks": None, "status": "cancelled", "trace": entries, "flags": flags}
    start = parse_ms(answer.get("start", answer.get("start_ms")))
    end = parse_ms(answer.get("end", answer.get("end_ms")))
    return {"blocks": None, "time_hint": {"start_ms": start, "end_ms": end},
            "status": "running", "trace": entries, "flags": flags}


def chunks(lines: list, size: int, overlap: int) -> list[list]:
    """Consecutive chunks of `size` lines, each sharing `overlap` lines with the one before, so a
    product talked about across a chunk boundary is seen whole in at least one chunk. Blocks found
    twice in the overlap are joined by `_merge`."""
    if len(lines) <= size:
        return [lines] if lines else []
    step = max(1, size - overlap)
    out = [lines[i:i + size] for i in range(0, len(lines) - overlap, step)]
    return [c for c in out if c]


def _merge(blocks: list[Block], gap_ms: int = 5000) -> list[Block]:
    """Join blocks that touch or nearly touch (one stretch of selling split across two chunks)."""
    out: list[Block] = []
    for b in sorted(blocks, key=lambda b: b.start_ms):
        if out and b.start_ms <= out[-1].end_ms + gap_ms:
            last = out[-1]
            conf = "high" if "high" in (last.confidence, b.confidence) else "medium"
            out[-1] = last.model_copy(update={"end_ms": max(last.end_ms, b.end_ms), "confidence": conf,
                                              "evidence": list(dict.fromkeys(last.evidence + b.evidence)),
                                              "line_ids": sorted(set(last.line_ids + b.line_ids))})
        else:
            out.append(b)
    return out


def refine_words(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """5. Re-transcribe only the product blocks with word timestamps → clauses (exact cut points)."""
    ctx = runtime.context
    blocks = load(ctx, state, "blocks", ProductBlocks)
    ranges = [TimeRange(start_ms=b.start_ms, end_ms=b.end_ms) for b in blocks.blocks]
    clauses = ctx.asr.clauses(blocks.asset_id, ranges)
    if clauses.lines:
        return {"clauses": ctx.store.put("clauses", clauses)}
    # Word timestamps came back empty (an ASR hiccup on these ranges): use the line-level transcript
    # inside the blocks. Cuts are less exact, but the run goes on, flagged, instead of feeding the
    # quote agent nothing.
    transcript = load(ctx, state, "transcript", Transcript)
    inside = [ln for ln in transcript.lines if any(r.start_ms <= ln.start_ms < r.end_ms for r in ranges)]
    fallback = Transcript(asset_id=blocks.asset_id, model=f"{transcript.model} (lines, no word timestamps)",
                          lines=inside)
    return {"clauses": ctx.store.put("clauses", fallback), "flags": ["no_word_timestamps:using_line_transcript"]}


def look(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """6. 画面理解 Agent over frames inside the blocks; drops blocks that mostly show another item.
    If the vision model is unavailable the run continues without picture checks, flagged."""
    ctx = runtime.context
    req = request(ctx, state)
    blocks = load(ctx, state, "blocks", ProductBlocks)
    ranges = [TimeRange(start_ms=b.start_ms, end_ms=b.end_ms) for b in blocks.blocks]
    frames = ctx.frames.sample(blocks.asset_id, ranges, ctx.limits.frame_every_ms)
    refs = ctx.library.reference_images(req.product.name)
    tags, entries, flags = [], [], []
    if not frames:  # nothing to look at: picture checks are skipped, and the reviewer is told so
        flags.append("no_frames:picture_checks_skipped")
    step = ctx.limits.frames_per_call
    inputs = [VisionInput(product=req.product.name, category=req.product.category, colors=req.product.colors,
                          frames=frames[i:i + step], reference_paths=refs) for i in range(0, len(frames), step)]
    results = VisionAgent().run_many(inputs, ctx.models, ctx.store, ctx.limits.model_concurrency)
    for inp, result in zip(inputs, results, strict=True):
        if isinstance(result, AgentFailed):  # those frames stay untagged; the run goes on, flagged
            flags.append(f"vision_degraded:{inp.frames[0].at_ms}-{inp.frames[-1].at_ms}:{result}")
            continue
        tags += to_frame_tags(inp, result.output).tags
        entries.append(trace("look", result))
    frame_tags = FrameTags(product=req.product.name, tags=tags)

    kept = []
    for b in blocks.blocks:
        inside = [t for t in tags if b.start_ms <= t.at_ms <= b.end_ms]
        other = sum(t.same_item == "no" for t in inside)
        if inside and other > len(inside) / 2:
            flags.append(f"block_dropped:{b.start_ms}-{b.end_ms}:other_item")
        else:
            kept.append(b)
    out = {"frame_tags": ctx.store.put("frame_tags", frame_tags), "trace": entries, "flags": flags}
    if len(kept) != len(blocks.blocks):
        out["blocks"] = ctx.store.put("blocks", blocks.model_copy(update={"blocks": kept}))
    return out


def select_quotes(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """7. 脚本生成 Agent (slice task): pick the anchor's own clauses, checked by code."""
    ctx = runtime.context
    req = request(ctx, state)
    clauses = load(ctx, state, "clauses", Transcript)
    tags = load(ctx, state, "frame_tags", FrameTags).tags if state.get("frame_tags") else []
    r = ctx.rules
    inp = SelectInput(
        product=req.product, asset_id=clauses.asset_id, clauses=clauses.lines, tags=tags,
        rules=QuoteRules(target_ms=req.options.target_ms, focus=req.options.focus,
                         hook_style=req.options.hook_style, hook_keywords=req.options.hook_keywords,
                         must_mention=req.options.must_mention, opening_connectives=r.opening_connectives,
                         hesitations=r.hesitations, claim_terms=r.claim_terms, min_quote_ms=r.min_quote_ms,
                         max_quotes=r.max_quotes, max_support=r.max_support_points, max_per_point=r.max_per_point,
                         live_room_phrases=r.live_room_phrases, opening_hedges=r.opening_hedges,
                         max_quote_ms=r.max_quote_ms, deictic=r.deictic),
        rework_notes=rework_notes(ctx, state, "content"),
    )
    result = SelectQuotesAgent().run_cached(inp, ctx.models, ctx.store)
    return {"quotes": ctx.store.put("quotes", to_quotes(inp, result.output)),
            "trace": [trace("select_quotes", result)], "flags": warning_flags("select_quotes", result)}
