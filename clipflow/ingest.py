"""Material intake (phase 4): what to file in the library for one media file. Engine code only; the
`ingest` workflow (workflows/ingest.py) runs these steps as a platform task and publishes the result.

- A replay: transcribe it, then ask the 商品定位 Agent, product by product, where it is sold. The same
  agent, chunks and merging as `locate_product` in a clip run, so their answers are shared through
  the cache. Each found stretch becomes a `sell_block` segment with the quotes that identify it.
- Product footage (one short clip of a known product and colour): sample a frame every 2 s, have the
  画面理解 Agent tag them, and keep each run of frames that clearly show this product as a `shot`
  segment, with its shot type and colour.

Model answers are cached by input (the agents' store), so a retry repeats no model call.
"""
from collections import Counter
from collections.abc import Callable

from .agents import AgentFailed, LocateAgent, VisionAgent
from .agents.locate import LocateInput, to_blocks
from .agents.vision import VisionInput
from .contracts import Product, TimeRange
from .graph.context import RunContext
from .library.store import Segment
from .nodes.slice import _merge as merge_blocks
from .nodes.slice import chunks

FRAME_EVERY_MS = 2000
FRAMES_PER_CALL = 6
SETTLE_MS = 1000  # the first second of a hand-held clip is the camera settling
MIN_SHOT_MS = 1500


class IngestFailed(RuntimeError):
    pass


def sell_blocks(ctx: RunContext, asset: str, products: list[Product],
                check: Callable[[], None] = lambda: None,
                step: Callable[[str], None] = lambda name: None) -> tuple[list[Segment], list[str]]:
    """(segments, names of products not found in this replay)."""
    step("转写")
    transcript = ctx.asr.transcribe(asset)
    segments, missing = [], []
    for product in products:
        check()
        step(f"定位商品：{product.name}")
        inputs = [LocateInput(product=product, asset_id=asset, lines=chunk)
                  for chunk in chunks(transcript.lines, ctx.limits.locate_lines_per_call,
                                      ctx.limits.locate_overlap_lines)]
        found = []
        for inp, result in zip(inputs, LocateAgent().run_many(inputs, ctx.models, ctx.store,
                                                               ctx.limits.model_concurrency), strict=True):
            if isinstance(result, AgentFailed):  # a missed chunk would silently lose a block: fail, retry later
                where = f"{product.name}，第 {inp.lines[0].i}–{inp.lines[-1].i} 行"
                raise IngestFailed(f"商品定位失败（{where}）：{result}")
            found += [b for b in to_blocks(inp, result.output).blocks if b.confidence != "low"]
        blocks = merge_blocks(found)
        if not blocks:
            missing.append(product.name)
        for b in blocks:
            said = "".join(ln.text for ln in transcript.lines if ln.end_ms > b.start_ms and ln.start_ms < b.end_ms)
            segments.append(Segment(product=product.name, kind="sell_block", start_ms=b.start_ms, end_ms=b.end_ms,
                                    confidence=b.confidence, evidence=b.evidence, text=said[:2000]))
    if segments:
        # Word timestamps for the selling stretches now, while nobody waits: a clip run's refine_words
        # step then finds them cached instead of spending minutes on the GPU (the ASR keeps them by window).
        check()
        step("逐字时间戳")
        ctx.asr.clauses(asset, [TimeRange(start_ms=s.start_ms, end_ms=s.end_ms) for s in segments])
    return segments, missing


def footage_shots(ctx: RunContext, asset: str, product: Product, color: str | None, duration_ms: int,
                  references: list[str] = (), check: Callable[[], None] = lambda: None,
                  step: Callable[[str], None] = lambda name: None) -> list[Segment]:
    """The stretches of one clip that clearly show this product."""
    if duration_ms <= SETTLE_MS + MIN_SHOT_MS:
        return []
    step("抽帧")
    # end a little before the last frame: a grid time exactly at the end of the file has no picture
    frames = ctx.frames.sample(asset, [TimeRange(start_ms=SETTLE_MS, end_ms=duration_ms - 300)], FRAME_EVERY_MS)
    check()
    step("画面理解")
    batches = [frames[i:i + FRAMES_PER_CALL] for i in range(0, len(frames), FRAMES_PER_CALL)]
    inputs = [VisionInput(product=product.name, category=product.category, colors=product.colors, frames=b,
                          reference_paths=list(references)) for b in batches]
    tags = {}
    for result in VisionAgent().run_many(inputs, ctx.models, ctx.store, ctx.limits.model_concurrency):
        if isinstance(result, AgentFailed):
            raise IngestFailed(f"画面理解失败：{result}")
        tags |= {t.index: t for t in result.output.tags}

    runs, current = [], []
    for f in frames:  # consecutive frames that show this product make one shot
        t = tags.get(f.index)
        if t and t.product_visible != "none" and t.same_item != "no":
            current.append((f, t))
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    half = FRAME_EVERY_MS // 2
    out = []
    for run in runs:
        start = max(SETTLE_MS, run[0][0].at_ms - half)
        end = min(duration_ms, run[-1][0].at_ms + half)
        if end - start < MIN_SHOT_MS:
            continue
        kinds = Counter(t.shot_type for _, t in run)
        seen_colors = Counter(t.color for _, t in run if t.color)
        clear = sum(t.product_visible == "clear" for _, t in run)
        out.append(Segment(
            product=product.name, kind="shot", start_ms=start, end_ms=end, shot_type=kinds.most_common(1)[0][0],
            color=color or (seen_colors.most_common(1)[0][0] if seen_colors else None),
            quality="good" if clear * 2 >= len(run) else "ok",
            frames=[{"at_ms": f.at_ms, "path": f.path, "shot_type": t.shot_type, "product_visible": t.product_visible,
                     "same_item": t.same_item, "color": t.color} for f, t in run]))
    return out


def probe_ms(path: str) -> int:
    """The file's duration (ffprobe)."""
    from .workers.pilot import common, path_of

    return int(round(float(common.probe(path_of(path))["format"]["duration"]) * 1000))
