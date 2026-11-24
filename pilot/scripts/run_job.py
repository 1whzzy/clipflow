"""Run one video job end to end, with the LLM gateway as the agent. No Claude Code needed.

  python pilot/scripts/run_job.py jobs/示例裤A-001.yaml [--model gpt-6.2-sol] [--no-render]
                                  [--rerender] [--no-vision]

Steps:
  1. transcripts   reuse pilot/work/<video>/transcript.json, or transcribe the source
  2. locate        if a replay has no start/end, the LLM finds where the product is being sold
  3. words         re-transcribe only the product blocks with word timestamps (exact cuts, captions)
  4. look          a vision model tags frames of the blocks and the product shots (what's on screen)
  5. pick          the LLM picks segments and, per segment, a product shot to cover it when the
                   live picture doesn't show what's being said; code checks hook, visuals, claims,
                   hesitations, length; the LLM fixes what fails
  6. fix captions  ASR corrections, only to words found in the product file or the transcript
  7. render        cut, fit to 1080x1920, cover shots, captions (off the legs when the pants are
                   on screen), end card with name / colors / verified price, −14 LUFS
  8. QA            renders/<job>/qa.md (the review checklist, filled in) and review_sheet.jpg

Everything the agent decided is saved in <output>/run.json; --rerender renders again from it
with no model calls.
"""
import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

import common
import cues as cuelib
import qa
import vision
from llm import LLM, env_value

W, H = 1080, 1920
FADE = 0.08
PAD = 0.12
CHUNK_LINES = 600
CONTEXT = 15  # seconds of transcript shown around each located block

POINTS = ["钩子", "版型", "颜色", "搭配", "面料", "尺码", "价格", "下单", "其他"]


# ---------- inputs ----------

def load_config():
    return yaml.safe_load((common.ROOT / "config.yaml").read_text(encoding="utf-8"))


def load_job(path):
    job = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    product_file = common.ROOT / "products" / f"{job['product']}.yaml"
    product = yaml.safe_load(product_file.read_text(encoding="utf-8")) if product_file.exists() else {"name": job["product"]}
    return job, product


def product_brief(p):
    parts = [f"款名：{p.get('name')}"]
    for key, label in [("category", "品类"), ("price", "直播价"), ("colors", "颜色"), ("sizes", "尺码"),
                       ("selling_points", "卖点"), ("verified_claims", "已核实的材质/工艺"),
                       ("anchor", "主播"), ("aliases", "别名")]:
        v = p.get(key)
        if v:
            parts.append(f"{label}：{'、'.join(map(str, v)) if isinstance(v, list) else v}")
    if not p.get("verified_claims"):
        parts.append("已核实的材质/工艺：无（不要用讲材质、工艺的句子）")
    return "\n".join(parts)


def norm_color(text):
    return text.replace("炭", "碳")


def protected_terms(cfg, job, product):
    terms = set(cfg["rules"].get("protected_terms", []))
    for key in ("colors", "selling_points", "aliases"):
        terms.update(map(str, product.get(key) or []))
    terms.add(str(product.get("name", "")))
    if product.get("category"):
        terms.add(str(product["category"]))
    req = job.get("requirements", {})
    terms.update(req.get("must_mention", []))
    terms.update(req.get("hook_keywords", []))
    for c in list(terms):
        terms.add(norm_color(c))
    return sorted((t for t in terms if len(t) >= 2), key=len, reverse=True)


def ensure_transcript(video, start=None, end=None):
    path = common.ROOT / "pilot" / "work" / video.stem / "transcript.json"
    if not path.exists():
        print(f"  transcribing {video.name} ...")
        cmd = [sys.executable, str(Path(__file__).with_name("transcribe.py")), str(video)]
        if start is not None:
            cmd += ["--start", str(max(0, start - 60))]
        if end is not None:
            cmd += ["--end", str(end + 60)]
        subprocess.run(cmd, check=True)
    return common.load_json(path)["segments"]


def lines_text(rows, start=None, end=None):
    return "\n".join(f"[{r['start']:.1f}-{r['end']:.1f}] {r['text']}" for r in rows
                     if (start is None or r["end"] >= start) and (end is None or r["start"] <= end))


def merge_ranges(ranges, gap=2.0):
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 1), round(b, 1)) for a, b in out]


def block_ranges(blocks):
    return merge_ranges([(max(0.0, b["start"] - CONTEXT), b["end"] + CONTEXT) for b in blocks])


# ---------- step 2: locate ----------

LOCATE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["blocks"],
    "properties": {"blocks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["start", "end", "confidence", "evidence"],
        "properties": {
            "start": {"type": "number", "description": "秒"},
            "end": {"type": "number", "description": "秒"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence": {"type": "string", "description": "为什么认为在讲这件商品（引用原话）"}}}}},
}


def locate(llm, product, rows):
    blocks = []
    for i in range(0, len(rows), CHUNK_LINES):
        chunk = rows[i:i + CHUNK_LINES]
        lo, hi = chunk[0]["start"], chunk[-1]["end"]
        system = ("你在帮短视频剪辑团队整理直播回放。主播通常不说商品款名，而是说“这条裤子”、价格、颜色、版型和面料。"
                  "根据商品资料判断转写中哪些时间段在讲解这件商品。同一场直播会卖多件相似商品，只有颜色、版型、面料、价格等对得上才算。")
        user = (f"商品资料：\n{product_brief(product)}\n\n"
                f"下面是直播转写的一段（{lo:.0f}–{hi:.0f} 秒）。找出主播在讲解这件商品的连续时间段（可以没有）。"
                "每段给出起止秒数、把握程度和原话证据，然后调用 submit_blocks。\n\n" + lines_text(chunk))

        def validate(args, lo=lo, hi=hi):
            problems = []
            for b in args["blocks"]:
                if not (lo - 1 <= b["start"] < b["end"] <= hi + 1):
                    problems.append(f"时间段 {b['start']}-{b['end']} 不在本段转写范围 {lo:.0f}-{hi:.0f} 内或起止颠倒")
            return problems

        args = llm.submit(system, user, "submit_blocks", "提交讲解这件商品的时间段", LOCATE_SCHEMA,
                          validate, label=f"locate {lo:.0f}-{hi:.0f}")
        for b in args["blocks"]:
            print(f"    {b['confidence']:6} {common.fmt_ts(b['start'])}-{common.fmt_ts(b['end'])}  {b['evidence'][:60]}")
        blocks += args["blocks"]
    keep = [b for b in blocks if b["confidence"] in ("high", "medium")]
    return sorted(keep, key=lambda b: b["start"])


# ---------- step 3: word timestamps for the blocks ----------

def ensure_words(video, ranges, offline=False):
    """Word-level transcript of just the product blocks (pilot/work/<video>/words.json).
    Only windows not transcribed yet are sent to the ASR."""
    path = common.ROOT / "pilot" / "work" / video.stem / "words.json"
    data = common.load_json(path) if path.exists() else {"windows": [], "segments": []}
    have = data.get("windows", [])
    missing = [(a, b) for a, b in ranges if not any(x <= a and b <= y for x, y in have)]
    if missing and offline:
        sys.exit(f"{path} is missing windows {missing}; run without --rerender first.")
    if missing:
        print(f"  {video.name}: word timestamps for {len(missing)} window(s), "
              f"{sum(b - a for a, b in missing):.0f}s of audio")
        spec = ",".join(f"{a:.1f}-{b:.1f}" for a, b in missing)
        subprocess.run([sys.executable, str(Path(__file__).with_name("transcribe.py")), str(video),
                        "--windows", spec, "--words", "--name", "words_new"], check=True)
        new_path = path.with_name("words_new.json")
        new = common.load_json(new_path)
        keep = [r for r in data["segments"] if not any(a <= r["start"] < b for a, b in missing)]
        # windows read from JSON are lists, new ones are tuples: make them one type before sorting
        data = {"source": str(video), "windows": merge_ranges([tuple(w) for w in have] + missing, gap=0),
                "segments": sorted(keep + new["segments"], key=lambda r: r["start"])}
        common.save_json(path, data)
        new_path.unlink()
        new_path.with_suffix(".txt").unlink(missing_ok=True)
    return clauses_from_rows(data["segments"])


PROMPT_ECHOES = ("请使用简体中文", "以下是直播带货的普通话内容")


def is_prompt_echo(text):
    """Whisper sometimes 'hears' its own initial prompt over music or silence."""
    rest = text
    for p in PROMPT_ECHOES:
        rest = rest.replace(p, "")
    return not re.sub(r"[\s，。！？、,.!?;；:：]", "", rest)


def clauses_from_rows(rows):
    """Split word-timed rows into clauses at punctuation: the unit the agent picks and cuts on."""
    out = []

    def close(ws):
        text = "".join(w["word"] for w in ws).strip()
        if text:
            out.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": text, "words": ws})

    for r in rows:
        if is_prompt_echo(r["text"]):
            continue
        words = r.get("words")
        if not words:
            out.append({"start": r["start"], "end": r["end"], "text": r["text"], "words": None})
            continue
        cur = []
        for w in words:
            cur.append(w)
            if w["word"].strip()[-1:] in "，。！？,.!?；;":
                close(cur)
                cur = []
        if cur:
            close(cur)
    return out


# ---------- step 4: look ----------

def live_tags(src, a, b):
    tags = vision.frames_between(src["timeline"], a, b)
    if not tags and src["timeline"]:
        mid = (a + b) / 2
        t = min(src["timeline"], key=lambda x: abs(x - mid))
        if abs(t - mid) <= 1.5:
            tags = [src["timeline"][t]]
    return tags


def check_blocks(src):
    """Drop blocks where the vision model says the pants on screen are a different item."""
    keep = []
    for b in src["blocks"]:
        tags = [t for t in vision.frames_between(src["timeline"], b["start"], b["end"]) if t["pants"] != "none"]
        yes, no = sum(t["same"] == "yes" for t in tags), sum(t["same"] == "no" for t in tags)
        b["vision"] = f"同款 {yes} / 不同款 {no} / 共 {len(tags)} 帧"
        if len(tags) >= 4 and no >= 0.6 * len(tags):
            print(f"  dropped {common.fmt_ts(b['start'])}-{common.fmt_ts(b['end'])}: {b['vision']}")
            continue
        keep.append(b)
    return keep


# ---------- step 5: pick segments ----------

PICK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["segments"],
    "properties": {"segments": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["source", "from_line", "to_line", "role", "point", "hook", "sees",
                     "cover_shot", "cover_from", "cover_at", "cover_length", "reason"],
        "properties": {
            "source": {"type": "string", "description": "素材编号，如 S1"},
            "from_line": {"type": "integer", "description": "第一句的行号 L"},
            "to_line": {"type": "integer", "description": "最后一句的行号 L（含）"},
            "role": {"type": "string", "enum": ["opening", "middle", "ending"]},
            "point": {"type": "string", "enum": POINTS, "description": "这段讲的卖点"},
            "hook": {"type": "string", "description": "开头段的钩子类型，其它段留空"},
            "sees": {"type": "string", "description": "观众在这段能看到的具体细节，如：碳灰色裤脚轮廓、裤脚和鞋面同框"},
            "cover_shot": {"type": "string", "description": "盖在这段上的同款实拍编号，如 B3；不用写空字符串"},
            "cover_from": {"type": "number", "description": "从实拍的第几秒开始用"},
            "cover_at": {"type": "number", "description": "在这段的第几秒盖上（相对段开头）"},
            "cover_length": {"type": "number", "description": "盖多少秒；不用写 0"},
            "reason": {"type": "string"}}}}},
}


def seg_text(src, s):
    return "".join(c["text"] for c in src["clauses"][s["from_line"]:s["to_line"] + 1])


def coverage(seg, src, shot, pred):
    """Share of the segment's time where the viewer sees what `pred` asks for."""
    dur = seg["end"] - seg["start"]
    covered = 0.0
    if shot and seg["cover_length"] > 0 and any(pred(f) for f in shot["frames"]):
        covered = seg["cover_length"]
    live = []
    for t, tags in sorted(src["timeline"].items()):
        if seg["start"] <= t <= seg["end"]:
            local = t - seg["start"]
            if not (shot and seg["cover_at"] <= local <= seg["cover_at"] + seg["cover_length"]):
                live.append(tags)
    live_time = dur - (seg["cover_length"] if shot else 0)
    if live:
        covered += live_time * sum(pred(t) for t in live) / len(live)
    return covered / dur if dur > 0 else 0.0


def opening_shows_pants(seg, src, shot):
    if shot and seg["cover_at"] <= 0.05 and seg["cover_length"] >= min(2.5, seg["end"] - seg["start"]):
        return any(vision.shows_pants(f) for f in shot["frames"]) and \
            not all(f["same"] == "no" for f in shot["frames"])
    first = live_tags(src, seg["start"], seg["start"] + 3.0)
    return bool(first) and vision.shows_pants(first[0]) and \
        sum(vision.shows_pants(t) for t in first) >= 2 / 3 * len(first)


def shot_color(shot, product):
    """The shot's color: the folder name when it names a product color (material/<款名>/炭灰色/),
    because vision models see charcoal as black; otherwise what the vision model saw."""
    folder = norm_color(Path(shot["file"]).parent.name)
    for c in product.get("colors") or []:
        if norm_color(str(c)).rstrip("色") in folder:
            return norm_color(str(c))
    return norm_color(shot["summary"])


HANGING_ENDS = ("的", "就", "然后", "但是", "因为", "所以", "而且", "那个", "这个", "我们", "就是", "要", "是", "把", "跟")


def unfinished(src, s):
    """Whisper ends almost every clause with a comma, so a comma alone means nothing, and livestream
    speech has no pauses to go by (tried: it flagged complete sentences). A segment counts as cut
    off only when it ends on a word that needs more after it."""
    text = re.sub(r"[\s，。！？、,.!?;；]+$", "", s["text"])
    for w in HANGING_ENDS:
        if text.endswith(w):
            return f"以“{w}”结尾"
    return None


def on_body_pants(f):
    return vision.shows_pants(f) and f["shot"] in ("full_body", "lower_body")


def auto_cover(seg, shot_ids, pred, product, used=()):
    """The best same-item product shot whose frames satisfy `pred`, in a color that matches what the
    segment says, to cover the segment from 0 s. Unused shots first, walking preferred.
    Returns {'shot','from','length'} or None."""
    dur = seg["end"] - seg["start"]
    said = [norm_color(c) for c in product.get("colors") or [] if norm_color(c) in norm_color(seg.get("text", ""))]
    best = None
    for k, s in shot_ids.items():
        frames = s.get("frames") or []
        if not frames or any(f["same"] == "no" for f in frames) or s["duration"] < 1.5:
            continue
        if said and not any(c in shot_color(s, product) for c in said):
            continue
        good = [f for f in frames if pred(f)]
        if not good:
            continue
        score = (k not in used, len(good), any(f["action"] == "walking" for f in frames), min(s["duration"], dur))
        if best is None or score > best[0]:
            best = (score, k, s)
    if not best:
        return None
    _, k, s = best
    start = 0.3 if s["duration"] > dur + 0.3 else 0.0
    return {"shot": k, "from": start, "length": round(min(dur, s["duration"] - start), 2)}


def try_cover(seg, src, shot_ids, pred, product, used, need_share, check=None):
    """Lay a matching product shot over a segment whose live picture fails. Keeps the change only if
    the segment then passes. Returns True when the segment passes (with or without a cover)."""
    c = auto_cover(seg, shot_ids, pred, product, used)
    if not c:
        return False
    old = {k: seg[k] for k in ("cover_shot", "cover_from", "cover_at", "cover_length")}
    seg.update(cover_shot=c["shot"], cover_from=c["from"], cover_at=0.0, cover_length=c["length"])
    shot = shot_ids[c["shot"]]
    ok = check(seg, src, shot) if check else coverage(seg, src, shot, pred) >= need_share
    if ok:
        seg["auto_cover"] = True
        used.add(c["shot"])
    else:
        seg.update(old)
    return ok


def pick(llm, job, product, sources, shots, cfg, use_vision):
    rules = cfg["rules"]
    req = job.get("requirements", {})
    lo_len, hi_len = req.get("length", [30, 45])
    hold = cfg["end_card"]["hold"]
    hook_words = req.get("hook_keywords") or req.get("must_mention") or []
    price = str(product.get("price") or "")
    verified = set(product.get("verified_claims") or [])
    shot_ids = {f"B{n}": s for n, s in enumerate(shots, 1)}

    material = []
    for sid, src in sources.items():
        allowed = set()
        for a, b in block_ranges(src["blocks"]):
            idx = [i for i, c in enumerate(src["clauses"]) if c["end"] >= a and c["start"] <= b]
            if not idx:
                continue
            allowed.update(idx)
            lines = []
            for i in idx:
                c = src["clauses"][i]
                look = f"  ‖ {vision.summary(live_tags(src, c['start'], c['end']))}" if use_vision else ""
                lines.append(f"L{i} [{c['start']:.1f}-{c['end']:.1f}] {c['text']}{look}")
            material.append(f"### 素材 {sid}，{a:.0f}–{b:.0f} 秒\n" + "\n".join(lines))
        src["allowed"] = allowed
    shot_list = "\n".join(f"{k} {Path(s['file']).name} {s['duration']:.1f}秒 颜色:{shot_color(s, product)} ‖ {s['summary']}"
                          for k, s in shot_ids.items()) or "（没有）"

    system = (
        "你是带货短视频剪辑师。从直播转写里挑句子，拼成一条完整的带货短视频。每行是一句话：行号、起止秒、原话，"
        "‖ 后面是那一刻画面里能看到什么（由视觉模型标注）。\n"
        "规则：\n"
        "1. 开头 3–8 秒。第一句单独听就能懂，不能以“而且、然后、所以、但是、就是”等承接词开头，直接点出最核心的卖点"
        f"（{'、'.join(hook_words) or '版型'}）。前 3 秒画面必须看得到穿着的裤腿和裤脚：选画面标注是"
        "“全身/下半身”且“裤子清楚”或“见裤脚”的句子。\n"
        "2. 每个卖点都要有对应画面：讲版型、颜色时看得清裤型；讲搭鞋时裤脚和鞋面同框或抬腿；讲面料时看得到面料细节或翻内侧。"
        "原片画面不对，就用同款实拍盖在这段上（原声继续），颜色要对得上；没有合适的实拍，就换一句或删掉这个卖点。\n"
        "3. 建议顺序：开头碳灰色上身看清裤型 → 走动、裤脚、搭鞋 → 一条完整的选码建议（适用人群+结论）→ 结尾讲颜色和价格。\n"
        "4. 删掉空泛、意思不明确的口播，删掉犹豫句和说一半的句子。“你看”这类句子只有画面看得清效果时才用。\n"
        "5. 工艺、材质只能说商品资料里“已核实”的；价格以商品资料为准，拿不准的报价句不用。\n"
        "6. 片段用整句（from_line 到 to_line 连续的行），可以调整顺序，不能重复使用同一句。"
    )
    user = (f"商品资料：\n{product_brief(product)}\n\n"
            f"要求：总时长 {lo_len}–{hi_len} 秒（结尾会再定格 {hold} 秒显示款名、颜色、价格）；"
            f"钩子类型：{req.get('hook_style', 'any')}；尽量包含这些词：{'、'.join(req.get('must_mention', [])) or '无'}。\n\n"
            f"同款实拍（可以盖在任意一段上）：\n{shot_list}\n\n"
            + "\n\n".join(material) + "\n\n挑好后调用 submit_segments 提交（按播放顺序）。")

    def resolve(s):
        src = sources[s["source"]]
        cl = src["clauses"]
        s["start"], s["end"] = cl[s["from_line"]]["start"], cl[s["to_line"]]["end"]
        s["text"] = seg_text(src, s)
        s["resolved"] = True
        return src, shot_ids.get(s["cover_shot"]) if s["cover_shot"] else None

    def validate(args):
        segs, problems = args["segments"], []
        if not segs:
            return ["没有片段"]
        used = {}
        used_shots = {s["cover_shot"] for s in segs if s.get("cover_shot")}
        for n, s in enumerate(segs, 1):
            src = sources.get(s["source"])
            if not src:
                problems.append(f"第{n}段素材编号 {s['source']} 不存在")
                continue
            if not (0 <= s["from_line"] <= s["to_line"] < len(src["clauses"])):
                problems.append(f"第{n}段行号 L{s['from_line']}–L{s['to_line']} 无效")
                continue
            lines = set(range(s["from_line"], s["to_line"] + 1))
            if not lines <= src["allowed"]:
                problems.append(f"第{n}段用了不在给出材料里的行")
                continue
            for other_n, other in used.get(s["source"], []):
                if lines & other:
                    problems.append(f"第{n}段和第{other_n}段用了同一句")
            used.setdefault(s["source"], []).append((n, lines))
            src, shot = resolve(s)
            text, dur = s["text"], s["end"] - s["start"]
            if dur < 1.5:
                problems.append(f"第{n}段只有 {dur:.1f} 秒，太短")
            for w in rules["hesitations"]:
                if w in text:
                    problems.append(f"第{n}段有犹豫/口头禅“{w}”：{text[:30]}，换一句或截掉")
            for w in rules["claim_terms"]:
                if w in text and w not in verified:
                    problems.append(f"第{n}段提到“{w}”，商品资料没有核实这个材质/工艺，不能用：{text[:30]}")
            for m in re.finditer(r"(\d{2,4})\s*(块|元)", text):
                if price and m.group(1) != price:
                    problems.append(f"第{n}段报价 {m.group(0)} 和商品资料的价格 {price} 不一致")
            # cover shot
            if s["cover_shot"]:
                if not shot:
                    problems.append(f"第{n}段实拍编号 {s['cover_shot']} 不存在")
                else:
                    # trim small overruns instead of sending the whole answer back
                    s["cover_at"] = max(0.0, s["cover_at"])
                    s["cover_from"] = max(0.0, s["cover_from"])
                    s["cover_length"] = round(min(s["cover_length"], dur - s["cover_at"],
                                                  shot["duration"] - s["cover_from"]), 2)
                    if s["cover_length"] < 1.2:
                        problems.append(f"第{n}段实拍只盖 {s['cover_length']} 秒，至少 1.2 秒")
                    if s["cover_at"] < 0 or s["cover_at"] + s["cover_length"] > dur + 0.05:
                        problems.append(f"第{n}段实拍 {s['cover_at']}+{s['cover_length']} 秒超出这段的 {dur:.1f} 秒")
                    if s["cover_from"] < 0 or s["cover_from"] + s["cover_length"] > shot["duration"]:
                        problems.append(f"第{n}段实拍 {s['cover_shot']} 只有 {shot['duration']:.1f} 秒，"
                                        f"从 {s['cover_from']} 秒用 {s['cover_length']} 秒不够")
                    if use_vision and shot["frames"] and all(f["same"] == "no" for f in shot["frames"]):
                        problems.append(f"第{n}段实拍 {s['cover_shot']} 被标为不是同款")
                    said = [c for c in product.get("colors") or [] if norm_color(c) in norm_color(text)]
                    seen = shot_color(shot, product)
                    if use_vision and said and not any(norm_color(c) in seen for c in said) and \
                            any(norm_color(c) in seen for c in product.get("colors") or []):
                        problems.append(f"第{n}段讲{said[0]}，但实拍 {s['cover_shot']} 是别的颜色")
            elif s["cover_length"] > 0:
                problems.append(f"第{n}段没写实拍编号却给了盖的时长")
            if not use_vision:
                continue
            need = vision.POINT_NEEDS.get(s["point"])
            if need and coverage(s, src, shot, need[1]) < 0.4 and \
                    not try_cover(s, src, shot_ids, need[1], product, used_shots, 0.4):
                problems.append(f"第{n}段讲{s['point']}，但画面里看不到{need[0]}"
                                f"（原片：{vision.summary(live_tags(src, s['start'], s['end']))}），"
                                "同款实拍里也没有能补上的。换一句或删掉这个卖点")
            shot = shot_ids.get(s["cover_shot"]) if s["cover_shot"] else None
            if any(w in text for w in rules["deictic"]) and coverage(s, src, shot, vision.shows_pants) < 0.5 and \
                    not try_cover(s, src, shot_ids, vision.shows_pants, product, used_shots, 0.5):
                problems.append(f"第{n}段说“你看”，但画面看不清裤子，同款实拍也补不上，删掉这句")
        if any("resolved" not in s for s in segs):
            return problems  # bad source or line numbers: fix those first
        first = segs[0]
        if first["role"] != "opening":
            problems.append("第一段必须是 opening")
        else:
            dur = first["end"] - first["start"]
            if not 3 <= dur <= 8.5:
                problems.append(f"开头 {dur:.1f} 秒，应 3–8 秒")
            first_clause = sources[first["source"]]["clauses"][first["from_line"]]["text"]
            for w in rules["opening_connectives"]:
                if first_clause.startswith(w):
                    problems.append(f"开头第一句以“{w}”开头，承接上文，单独听不懂：{first_clause}")
                    break
            if hook_words and not any(w in first["text"] for w in hook_words):
                problems.append(f"开头没点出核心卖点（{'、'.join(hook_words)}）：{first['text'][:30]}")
            if use_vision:
                src, shot = resolve(first)
                if not opening_shows_pants(first, src, shot) and \
                        not try_cover(first, src, shot_ids, on_body_pants, product, used_shots, 0,
                                      check=opening_shows_pants):
                    problems.append("开头前 3 秒看不清裤型，也没有能盖住开头的同款上身实拍（原片："
                                    f"{vision.summary(live_tags(src, first['start'], first['start'] + 3))}）。"
                                    "换一句画面是全身/下半身的，或用同款上身实拍从第 0 秒盖住")
        last = segs[-1]
        if last["role"] != "ending":
            problems.append("最后一段必须是 ending")
        elif last["point"] not in ("价格", "下单", "颜色"):
            problems.append("结尾应讲价格、颜色或下单")
        if sum(s["point"] == "尺码" for s in segs) > 1:
            problems.append("选码建议只留一条完整的")
        for n, s in enumerate(segs, 1):
            if s["point"] in ("尺码",) or s["role"] == "ending":
                why = unfinished(sources[s["source"]], s)
                if why:
                    problems.append(f"第{n}段话没说完（{why}）：{s['text'][-20:]}")
        total = sum(s["end"] - s["start"] for s in segs) + hold
        if not (lo_len * 0.9 <= total <= hi_len * 1.1):
            problems.append(f"总时长 {total:.1f} 秒（含结尾定格 {hold} 秒），要求 {lo_len}–{hi_len} 秒")
        return problems

    segs = llm.submit(system, user, "submit_segments", "按播放顺序提交选好的片段", PICK_SCHEMA,
                      validate, max_attempts=4, label="pick")["segments"]
    for n, s in enumerate(segs, 1):
        if s.get("auto_cover"):
            s["reason"] += f"（原片画面不支持这个卖点，代码自动盖上同款实拍 {s['cover_shot']}）"
            print(f"  segment {n}: live picture doesn't show the {s['point']}; "
                  f"covered with {s['cover_shot']} for {s['cover_length']}s")
    for s in segs:
        src, shot = resolve(s)
        s["cover"] = ({"shot": s["cover_shot"], "file": shot["file"], "from": s["cover_from"],
                       "at": s["cover_at"], "length": s["cover_length"]} if shot else None)
        s["visual_live"] = vision.summary(live_tags(src, s["start"], s["end"])) if use_vision else "未看画面"
        s["visual_cover"] = shot["summary"] if shot else ""
    return pad_segments(segs, sources)


def visual_checks(segs, sources, shots_by_file, use_vision):
    """The same visual checks as in pick, recorded for the QA report."""
    if not use_vision:
        return {"opening": None, "segments": [{"need": "", "share": 0.0, "ok": None} for _ in segs]}
    out = []
    for s in segs:
        shot = shots_by_file.get(s["cover"]["file"]) if s.get("cover") else None
        need = vision.POINT_NEEDS.get(s["point"])
        if not need:
            out.append({"need": "", "share": 0.0, "ok": None})
            continue
        share = coverage(s, sources[s["source"]], shot, need[1])
        out.append({"need": need[0], "share": share, "ok": share >= 0.4})
    first = segs[0]
    shot = shots_by_file.get(first["cover"]["file"]) if first.get("cover") else None
    return {"opening": opening_shows_pants(first, sources[first["source"]], shot), "segments": out}


def pad_segments(segs, sources):
    """A little room at each edge so words aren't clipped, without reaching into the
    neighbouring clauses (which would let half a word through)."""
    for s in segs:
        cl = sources[s["source"]]["clauses"]
        prev_end = cl[s["from_line"] - 1]["end"] if s["from_line"] > 0 else 0.0
        next_start = cl[s["to_line"] + 1]["start"] if s["to_line"] + 1 < len(cl) else s["end"] + PAD
        s["start"] = round(min(s["start"], max(prev_end, s["start"] - PAD, 0.0)), 2)
        s["end"] = round(max(s["end"], min(next_start, s["end"] + PAD)), 2)
    return segs


# ---------- step 6: caption fixes ----------

FIX_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["fixes"],
    "properties": {"fixes": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["wrong", "right", "reason"],
        "properties": {"wrong": {"type": "string"}, "right": {"type": "string"}, "reason": {"type": "string"}}}}},
}


def fix_captions(llm, product, segs, sources, cfg):
    text = "\n".join(s["text"] for s in segs)
    brief = product_brief(product)
    heard = "".join(c["text"] for src in sources.values() for c in src["clauses"])
    verified = set(product.get("verified_claims") or [])
    system = ("你在校对直播转写生成的字幕。只改明显的语音识别错误：同音错字、数字、颜色、尺码。"
              "改成的词必须在商品资料里，或者在本场直播转写的其它地方原样出现过；不能按上下文猜词。"
              "不要改说话风格，不要删口语，不要润色。")
    user = (f"商品资料：\n{brief}\n\n字幕原文：\n{text}\n\n"
            "列出需要替换的词（wrong 必须是原文里出现的原样文字），调用 submit_fixes。没有就提交空列表。")

    def validate(args):
        problems = []
        for f in args["fixes"]:
            if not f["wrong"] or f["wrong"] not in text:
                problems.append(f"“{f['wrong']}” 不在字幕原文中")
            elif f["right"] and f["right"] not in brief and f["right"] not in heard:
                problems.append(f"“{f['right']}”在商品资料和本场转写里都没有，不能凭上下文改字；拿不准就不改")
            elif any(w in f["right"] and w not in verified for w in cfg["rules"]["claim_terms"]):
                problems.append(f"“{f['right']}”是未核实的材质/工艺，不能加进字幕")
        return problems

    fixes = llm.submit(system, user, "submit_fixes", "提交字幕替换", FIX_SCHEMA, validate, label="captions")["fixes"]
    return [f for f in fixes if f["wrong"] != f["right"]]


# ---------- step 7: captions + render ----------

def seg_chars(src, s):
    chars = []
    for c in src["clauses"][s["from_line"]:s["to_line"] + 1]:
        part = cuelib.chars_from_words(c["words"]) if c.get("words") else \
            cuelib.chars_from_text(c["text"], c["start"], c["end"])
        if part:
            part[-1]["hard"] = part[-1]["brk"] = True
        chars += part
    return chars


def build_cues(segs, sources, fixes, terms):
    cues, offset = [], 0.0
    for i, s in enumerate(segs):
        src = sources[s["source"]]
        chars = cuelib.apply_fixes(seg_chars(src, s), fixes)
        for c in cuelib.segment_cues(chars, s["start"], s["end"], offset, terms):
            c["seg"] = i
            cues.append(c)
        offset += s["end"] - s["start"]
    return cues


def place_cues(cues, segs, sources, shots_by_file, card_start):
    """Captions go to the top while the pants (or hem and shoes) are on screen, else bottom."""
    starts, t = [], 0.0
    for s in segs:
        starts.append(t)
        t += s["end"] - s["start"]
    for c in cues:
        s = segs[c["seg"]]
        local = (c["start"] + c["end"]) / 2 - starts[c["seg"]]
        cov = s.get("cover")
        if cov and cov["at"] <= local <= cov["at"] + cov["length"]:
            tags = shots_by_file.get(cov["file"], {}).get("frames", [])
        else:
            src = sources[s["source"]]
            tags = live_tags(src, s["start"] + local - 1.0, s["start"] + local + 1.0)
        legs = any(vision.shows_pants(x) or vision.shows_hem_shoes(x) for x in tags)
        c["pos"] = "top" if legs and c["end"] <= card_start else "bottom"  # the end card owns the top
    return cues


def end_card(product, job, total, cfg):
    lines = [str(product.get("name", ""))]
    colors = [norm_color(str(c)) for c in product.get("colors") or []]
    if colors:
        lines.append(" · ".join(colors))
    if product.get("price") and product.get("price_verified"):
        lines.append(f"直播价 ¥{product['price']}")
    cta = (job.get("requirements") or {}).get("cta")
    if cta:
        lines.append(str(cta))
    hold = cfg["end_card"]["hold"]
    return {"start": max(0.0, total + hold - cfg["end_card"]["seconds"]), "end": total + hold, "lines": lines}


def base_filter(src_w, src_h):
    if abs(src_w / src_h - W / H) / (W / H) < 0.05:  # already about 9:16: scale and crop the edge
        return f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,format=yuv420p[base]"
    return (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p[base]")


def render(segs, sources, cues, card, out_dir, cfg, with_captions=True):
    ffmpeg, enc = common.ffmpeg_bin(), common.video_encoder_args()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, s in enumerate(segs):
            src = sources[s["source"]]
            dur = s["end"] - s["start"]
            part = Path(tmp) / f"part{i:02d}.mp4"
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{s['start']:.3f}", "-t", f"{dur:.3f}", "-i", str(src["file"])]
            fc = base_filter(src["width"], src["height"])
            b = s.get("cover")
            if b:
                cmd += ["-ss", f"{b['from']:.3f}", "-t", f"{b['length']:.3f}", "-i", b["file"]]
                fc += (f";[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,format=yuv420p,"
                       f"setpts=PTS-STARTPTS+{b['at']}/TB[br];"
                       f"[base][br]overlay=0:0:eof_action=pass:enable='between(t,{b['at']},{b['at'] + b['length']})'[v]")
            else:
                fc += ";[base]null[v]"
            fc += (f";[0:a]afade=t=in:d={FADE},afade=t=out:st={max(0, dur - FADE):.3f}:d={FADE},"
                   f"aresample=48000[a]")
            common.run(cmd + ["-filter_complex", fc, "-map", "[v]", "-map", "[a]", *enc,
                              "-c:a", "aac", "-b:a", "160k", str(part)])
            parts.append(part)
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        cut = out_dir / "cut.mp4"
        common.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(listing), "-c", "copy", str(cut)])

    cuelib.write_srt(out_dir / "captions.srt", cues)
    cuelib.write_ass(out_dir / "captions.ass", cues if with_captions else [], card,
                     font=cfg["video"].get("caption_font", "Microsoft YaHei"),
                     top_margin=cfg["captions"]["top_margin"], bottom_margin=cfg["captions"]["bottom_margin"])
    hold = cfg["end_card"]["hold"]
    vf = f"tpad=stop_mode=clone:stop_duration={hold},subtitles=captions.ass"
    # Loudness to the output spec (-14 LUFS integrated, -1.5 dBTP), as for Douyin uploads.
    af = f"apad=pad_dur={hold},loudnorm=I=-14:TP=-1.5:LRA=11"
    common.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", "cut.mp4", "-vf", vf, *enc,
                "-af", af, "-ar", "48000", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", "final.mp4"], cwd=out_dir)
    return out_dir / "final.mp4"


# ---------- main ----------

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("job")
    p.add_argument("--model", help="override LLM_MODEL from .env")
    p.add_argument("--vision-model", help="override LLM_VISION_MODEL from .env")
    p.add_argument("--no-render", action="store_true", help="stop after the agent's decisions")
    p.add_argument("--rerender", action="store_true",
                   help="skip the models: reuse the decisions in <output>/run.json and only render again")
    p.add_argument("--keep-blocks", action="store_true",
                   help="reuse the product blocks from <output>/run.json (skip locate; caches stay valid)")
    p.add_argument("--no-vision", action="store_true",
                   help="don't look at frames (visual checks and cover shots are skipped)")
    args = p.parse_args()

    t0 = time.time()
    cfg = load_config()
    job, product = load_job(args.job)
    out_dir = common.ROOT / job.get("output", f"renders/{Path(args.job).stem}")
    llm = LLM(args.model)
    vllm = LLM(args.vision_model or env_value("LLM_VISION_MODEL") or llm.model)
    req = job.get("requirements", {})
    use_vision = not args.no_vision
    terms = protected_terms(cfg, job, product)
    print(f"Job {args.job}: {product.get('name')}, model {llm.model}, vision {vllm.model if use_vision else 'off'}")

    print("1. transcripts")
    sources = {}
    for n, r in enumerate(job["sources"]["replays"], 1):
        f = common.ROOT / r["file"]
        info = common.probe(f)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        start, end = common.parse_ts(r.get("start")), common.parse_ts(r.get("end"))
        rows = ensure_transcript(f, start, end)
        sources[f"S{n}"] = {"file": f, "rows": rows, "width": v["width"], "height": v["height"],
                            "given": (start, end), "timeline": {}}
        print(f"  S{n} {f.name}: {len(rows)} lines")

    previous = common.load_json(out_dir / "run.json") if args.rerender else None
    if previous:
        print("2. reusing the agent's decisions from run.json")
        for sid, s in sources.items():
            s["blocks"] = previous["sources"][sid]["blocks"]
    elif args.keep_blocks:
        path = out_dir / "blocks.json"
        kept = common.load_json(path)
        print(f"2. locate: reusing the blocks from {path.name}")
        for sid, s in sources.items():
            s["blocks"] = kept[sid]
    else:
        print("2. locate")
        for sid, s in sources.items():
            start, end = s["given"]
            if start is not None and end is not None:
                s["blocks"] = [{"start": start, "end": end, "confidence": "given", "evidence": "job file"}]
                print(f"  {sid}: using the job's range {common.fmt_ts(start)}-{common.fmt_ts(end)}")
            else:
                print(f"  {sid}: asking the model where {product.get('name')} is sold")
                s["blocks"] = locate(llm, product, s["rows"])
    if not any(s["blocks"] for s in sources.values()):
        sys.exit("The product wasn't found in any source. Give a start/end in the job file.")

    print("3. word timestamps")
    for sid, s in sources.items():
        s["clauses"] = ensure_words(s["file"], block_ranges(s["blocks"]), offline=bool(previous))
        print(f"  {sid}: {len(s['clauses'])} clauses")

    print("4. look")
    shots = vision.list_shots(job["sources"].get("shots")) if req.get("broll", True) else []
    tagger = None
    if use_vision and not previous:
        tagger = vision.Tagger(vllm, product, vision.reference_frames(shots))
    if use_vision:
        for sid, s in sources.items():
            s["timeline"] = vision.scan_video(tagger, s["file"], block_ranges(s["blocks"]),
                                              cfg["vision"]["step"])
            if not previous and not args.keep_blocks:
                s["blocks"] = check_blocks(s)
        vision.scan_shots(tagger, shots)
    else:
        print("  skipped (--no-vision): no visual checks, no cover shots")
        shots = []
    shots_by_file = {s["file"]: s for s in shots}
    if not previous:  # so a failed pick can be resumed with --keep-blocks
        out_dir.mkdir(parents=True, exist_ok=True)
        common.save_json(out_dir / "blocks.json", {sid: s["blocks"] for sid, s in sources.items()})

    if previous:
        segs, fixes = previous["segments"], previous["caption_fixes"]
    else:
        print("5. pick segments")
        segs = pick(llm, job, product, sources, shots, cfg, use_vision)
    for s in segs:
        cov = f" +{s['cover']['shot']}" if s.get("cover") else ""
        print(f"  {s['role']:8} {s['point']:3} {s['source']} {common.fmt_ts(s['start'])} "
              f"+{s['end'] - s['start']:4.1f}s{cov:5} {s['text'][:36]}")
    total = sum(s["end"] - s["start"] for s in segs)
    print(f"  total {total:.1f}s + {cfg['end_card']['hold']}s end card")

    if not previous:
        fixes = []
        if req.get("captions", True):
            print("6. caption fixes")
            fixes = fix_captions(llm, product, segs, sources, cfg)
            for f in fixes:
                print(f"  {f['wrong']} → {f['right']}  ({f['reason'][:40]})")
            if not fixes:
                print("  none")

    record = {
        "job": str(args.job), "product": product.get("name"), "model": llm.model,
        "vision_model": vllm.model if use_vision else None,
        "sources": {sid: {"file": str(s["file"]), "blocks": s["blocks"]} for sid, s in sources.items()},
        "segments": segs, "caption_fixes": fixes,
    }
    card = end_card(product, job, total, cfg)
    cues = place_cues(build_cues(segs, sources, fixes, terms), segs, sources, shots_by_file, card["start"])
    record["end_card"] = card
    record["captions"] = len(cues)

    if not args.no_render:
        print("7. render")
        final = render(segs, sources, cues, card, out_dir, cfg, req.get("captions", True))
        record["output"] = str(final)
        record["duration"] = float(common.probe(final)["format"]["duration"])
        print("8. QA")
        record["qa"] = qa.report(out_dir, record, segs, sources, cues, terms, product, job, cfg, use_vision,
                                 visual_checks(segs, sources, shots_by_file, use_vision), shots_by_file)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        cuelib.write_srt(out_dir / "captions.srt", cues)

    record["llm_usage"] = previous["llm_usage"] if previous else {"agent": llm.usage(), "vision": vllm.usage()}
    record["llm_calls"] = previous["llm_calls"] if previous else llm.calls + vllm.calls
    record["seconds"] = round(time.time() - t0, 1)
    common.save_json(out_dir / "run.json", record)
    print(f"\nDone in {record['seconds']:.0f}s.")
    for name, u in record["llm_usage"].items():
        if isinstance(u, dict):
            print(f"  {name}: {u['calls']} calls, {u['prompt_tokens']} in / {u['completion_tokens']} out tokens")
    if record.get("output"):
        print(f"Video: {record['output']} ({record['duration']:.1f}s)")
        print(f"QA:    {out_dir / 'qa.md'}  ({record['qa']['failed']} item(s) need attention)")
    print(f"Record: {out_dir / 'run.json'}")


if __name__ == "__main__":
    main()
