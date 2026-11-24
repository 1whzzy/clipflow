"""Pre-delivery check for a rendered job: the reviewer's checklist (交付前逐项回看), filled in.

Writes renders/<job>/qa.md and renders/<job>/review_sheet.jpg (frames at 0.5 / 1.5 / 2.5 s,
each segment's middle and the end card), and returns a summary for run.json.

Automatic checks can say ✗ for sure; a ✓ still needs a person to watch for the items marked 人工.
"""
import subprocess
import tempfile
from pathlib import Path

import common
import cues as cuelib
import vision

OK, BAD, HUMAN = "✅", "❌", "👀"


def frames_for_sheet(segs, total, hold):
    times = [0.5, 1.5, 2.5]
    t = 0.0
    for s in segs:
        d = s["end"] - s["start"]
        times.append(t + d / 2)
        t += d
    times.append(total + hold * 0.5)
    return [round(x, 2) for x in times]


def contact_sheet(video, times, out):
    with tempfile.TemporaryDirectory() as tmp:
        for n, t in enumerate(times):
            subprocess.run([common.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                            "-i", str(video), "-frames:v", "1", "-vf", "scale=270:480",
                            str(Path(tmp) / f"f{n:02d}.jpg")], check=True)
        cols = 4
        rows = -(-len(times) // cols)
        subprocess.run([common.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(Path(tmp) / "f%02d.jpg"), "-vf", f"tile={cols}x{rows}:padding=6:color=white",
                        "-frames:v", "1", str(out)], check=True)


def report(out_dir, record, segs, sources, cue_list, terms, product, job, cfg, use_vision, visual,
           shots_by_file=None):
    """visual: from run_job.visual_checks(): {'opening': bool|None, 'segments': [{'need','share','ok'}]}"""
    rules = cfg["rules"]
    req = job.get("requirements", {})
    hold = cfg["end_card"]["hold"]
    total = sum(s["end"] - s["start"] for s in segs)
    items = []

    def item(ok, text):
        items.append((ok, text))

    # 1. first 3 seconds
    first = segs[0]
    src = sources[first["source"]]
    first_clause = src["clauses"][first["from_line"]]["text"]
    if visual["opening"] is None:
        item(HUMAN, "静音看前 3 秒，能认出主推的是裤子（未看画面，需人工）")
    else:
        item(OK if visual["opening"] else BAD, "静音看前 3 秒，能认出主推的是裤子"
             + ("" if visual["opening"] else "：前 3 秒画面看不清裤型"))
    connective = next((w for w in rules["opening_connectives"] if first_clause.startswith(w)), None)
    item(BAD if connective else HUMAN,
         f"第一句可以独立听懂：「{first_clause}」" + (f"以“{connective}”开头" if connective else "（开声音听一遍）"))

    # 2. each selling point has a picture
    missing = [n for n, v in enumerate(visual["segments"], 1) if v["ok"] is False]
    if not use_vision:
        item(HUMAN, "每个保留的核心卖点都能指向一个具体画面（未看画面，需人工）")
    else:
        item(BAD if missing else OK, "每个保留的核心卖点都能指向一个具体画面"
             + (f"：第 {'、'.join(map(str, missing))} 段没有" if missing else ""))
    covers = [s for s in segs if s.get("cover")]
    auto = sum(bool(s.get("auto_cover")) for s in segs)
    item(HUMAN, f"新增的实拍能辨认同款和对应颜色（{len(covers)} 段用了实拍，其中 {auto} 段是代码自动补的，见下表）")
    worn = [s for s in shots_by_file.values()
            if any(vision.shows_pants(f) for f in s.get("frames", []))] if shots_by_file else []
    if use_vision and shots_by_file and not worn:
        item(BAD, f"素材缺口：{len(shots_by_file)} 条同款实拍里没有一条是真人上身（只有面料特写、挂拍、平铺），"
                  "开头无法按审片意见配“碳灰色正面上身和一步走动”，需要补拍")

    # 3. captions
    cp = cuelib.cue_problems(cue_list, terms)
    item(BAD if cp else OK, "核心词完整可读，没有单字闪现、没有拆词" + (f"：{len(cp)} 处" if cp else ""))
    said = "".join(c["text"].replace("\\N", "") for c in cue_list)
    hes = [w for w in rules["hesitations"] if w in said]
    item(BAD if hes else OK, "口播无残留犹豫句" + (f"：{'、'.join(hes)}" if hes else ""))
    top = sum(c.get("pos") == "top" for c in cue_list)
    item(HUMAN, f"展示裤型时字幕不挡大腿、膝部和裤脚（{top}/{len(cue_list)} 条字幕已移到上方）")

    # 4. ending facts
    verified = set(product.get("verified_claims") or [])
    claims = [w for w in rules["claim_terms"] if w in said and w not in verified]
    item(BAD if claims else OK, "工艺、材质有依据" + (f"：字幕里有未核实的“{'、'.join(claims)}”" if claims else ""))
    if product.get("price_verified"):
        item(OK, f"结尾价格已核实（¥{product.get('price')}，商品资料 price_verified: true）")
    else:
        item(BAD, f"价格未核实：商品资料 price_verified 不是 true，末屏没有显示价格；口播里的价格发布前要核实")
    cta = req.get("cta")
    item(HUMAN if cta else BAD, "购买提示与实际发布入口对应" + (f"：「{cta}」" if cta else "：任务单没有填 cta，末屏没有购买提示"))

    failed = sum(ok == BAD for ok, _ in items)
    lines = [f"# 交付前检查：{record['product']}", "",
             f"成片 `{Path(record['output']).name}`，{record['duration']:.1f} 秒（含结尾定格 {hold} 秒），"
             f"{len(segs)} 段，{len(cue_list)} 条字幕。{BAD} 必须处理，{HUMAN} 需要人看，{OK} 自动检查通过。", ""]
    lines += [f"- {ok} {text}" for ok, text in items]
    if cp:
        lines += ["", "## 字幕问题", ""] + [f"- {p}" for p in cp]
    lines += ["", "## 逐段：原声、镜头、观众看到的细节", "",
              "| # | 角色 | 卖点 | 原声时间码 | 原话 | 镜头 | 观众看到的细节 | 画面核对 |",
              "|---|---|---|---|---|---|---|---|"]
    for n, (s, v) in enumerate(zip(segs, visual["segments"]), 1):
        shot = (f"实拍 {s['cover']['shot']} {Path(s['cover']['file']).name} 第 {s['cover']['at']:.1f}s 起 "
                f"{s['cover']['length']:.1f}s（{s.get('visual_cover', '')}）；其余原片" if s.get("cover") else "原片")
        check = "—" if v["ok"] is None else (f"{OK} {v['need']} {v['share']:.0%}" if v["ok"]
                                             else f"{BAD} {v['need']} 只有 {v['share']:.0%}")
        lines.append(f"| {n} | {s['role']} | {s['point']} | {s['source']} {common.fmt_ts(s['start'])}–"
                     f"{common.fmt_ts(s['end'])} | {s['text'][:40]} | {shot}；原片画面：{s.get('visual_live', '')} | "
                     f"{s.get('sees', '')} | {check} |")
    fixes = record.get("caption_fixes") or []
    lines += ["", "## 字幕改字（需人工确认）", ""]
    lines += [f"- {f['wrong']} → {f['right']}：{f['reason']}" for f in fixes] or ["- 无"]
    card = record.get("end_card") or {}
    lines += ["", "## 末屏", "", f"- {' / '.join(card.get('lines', []))}（{card.get('start', 0):.1f}–{card.get('end', 0):.1f} 秒）",
              "", "## 抽帧", "", "`review_sheet.jpg`：0.5 / 1.5 / 2.5 秒、每段中间、末屏。", ""]
    (out_dir / "qa.md").write_text("\n".join(lines), encoding="utf-8")

    try:
        contact_sheet(out_dir / "final.mp4", frames_for_sheet(segs, total, hold), out_dir / "review_sheet.jpg")
    except subprocess.CalledProcessError as e:
        print(f"  contact sheet failed: {e}")
    for ok, text in items:
        print(f"  {ok} {text}")
    return {"failed": failed, "items": [{"status": ok, "text": text} for ok, text in items],
            "caption_problems": cp}
