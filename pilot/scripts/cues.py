"""Caption cues from word timestamps, plus the ASS file that is burned into the video.

Fixes from the review of the first pilot video:
  - "喇叭裤" was split over two captions and "裤" flashed for 0.125 s. Cues are now cut at
    phrase boundaries first (the transcript's punctuation), then at word boundaries to fit the
    width, never inside a protected term (product words such as 喇叭裤 / 微喇 / 碳灰), and cues
    shorter than MIN_SECONDS or MIN_CHARS are merged into a neighbour.
  - Captions covered the legs while the pants were on screen. Each cue now has a position:
    "top" when the picture shows the pants (from the vision tags), otherwise "bottom".
  - Caption times come from word timestamps, so a caption shows only what is said inside the
    cut, not the whole transcript line.

The text model is a list of characters, each with a start/end time and whether a caption may
break after it. Fixes (ASR corrections) replace characters and spread the original time.
"""
import re

PUNCT = set("，。！？、；：,.!?;:…~～ ")
MAX_LINE = 12        # characters per caption line at FONT_SIZE on a 1080-wide frame
MAX_CUE = 2 * MAX_LINE
MIN_SECONDS = 0.7
MIN_CHARS = 3
FONT_SIZE = 76       # px at PlayResY 1920


# ---------- text model ----------

def chars_from_words(words):
    """[{'word','start','end'}] → [{'c','s','e','brk','hard'}]. brk: a caption may break after it;
    hard: a phrase ends after it (punctuation followed)."""
    out = []
    for w in words:
        text = w["word"].strip()
        if not text:
            continue
        real = [ch for ch in text if ch not in PUNCT]
        n = max(1, len(real))
        step = (w["end"] - w["start"]) / n
        k = 0
        for ch in text:
            if ch in PUNCT:
                if out:
                    out[-1]["hard"] = True
                    out[-1]["brk"] = True
                continue
            out.append({"c": ch, "s": w["start"] + k * step, "e": w["start"] + (k + 1) * step,
                        "brk": False, "hard": False})
            k += 1
        if out:
            out[-1]["brk"] = True  # end of a word token
    return out


def chars_from_text(text, start, end):
    """Fallback without word timestamps: spread the time evenly; break anywhere except inside
    ASCII runs (numbers, sizes)."""
    out = []
    real = [ch for ch in text if ch not in PUNCT and not ch.isspace()]
    step = (end - start) / max(1, len(real))
    k = 0
    for ch in text:
        if ch in PUNCT or ch.isspace():
            if out:
                out[-1]["hard"] = out[-1]["brk"] = True
            continue
        out.append({"c": ch, "s": start + k * step, "e": start + (k + 1) * step,
                    "brk": not (ch.isascii() and ch.isalnum()), "hard": False})
        k += 1
    for a, b in zip(out, out[1:]):  # allow a break at the end of an ASCII run
        if a["c"].isascii() and a["c"].isalnum() and not (b["c"].isascii() and b["c"].isalnum()):
            a["brk"] = True
    if out:
        out[-1]["brk"] = out[-1]["hard"] = True
    return out


def text_of(chars):
    return "".join(c["c"] for c in chars)


def apply_fixes(chars, fixes):
    """Replace 'wrong' with 'right' (ASR corrections). New characters share the old span's time;
    a caption may break only at the end of the replaced span."""
    for f in fixes:
        wrong, right = f["wrong"], f["right"]
        wrong = "".join(ch for ch in wrong if ch not in PUNCT)
        right = "".join(ch for ch in right if ch not in PUNCT)
        if not wrong:
            continue
        i = text_of(chars).find(wrong)
        while i != -1:
            span = chars[i:i + len(wrong)]
            s, e = span[0]["s"], span[-1]["e"]
            step = (e - s) / max(1, len(right))
            new = [{"c": ch, "s": s + k * step, "e": s + (k + 1) * step, "brk": False, "hard": False}
                   for k, ch in enumerate(right)]
            if new:
                new[-1]["brk"], new[-1]["hard"] = span[-1]["brk"], span[-1]["hard"]
            chars[i:i + len(wrong)] = new
            i = text_of(chars).find(wrong, i + len(right))
    return chars


# ---------- cue building ----------

def protected_spans(text, terms):
    spans = []
    for term in terms:
        if len(term) < 2:
            continue
        for m in re.finditer(re.escape(term), text):
            spans.append((m.start(), m.end()))
    return spans


def inside_term(pos, spans):
    """True if a break after character index pos-1 (i.e. before pos) would cut a term."""
    return any(a < pos < b for a, b in spans)


def split_phrase(chars, terms, max_len=MAX_LINE):
    """Cut one phrase (no punctuation inside) into pieces of at most max_len characters,
    preferring word boundaries and never cutting a protected term."""
    pieces = []
    while len(chars) > max_len:
        text = text_of(chars)
        spans = protected_spans(text, terms)
        n_lines = -(-len(chars) // max_len)
        target = len(chars) / n_lines  # balanced lines read better than one full and one stub
        ok = [pos for pos in range(1, max_len + 1)  # break before chars[pos]
              if chars[pos - 1]["brk"] and not inside_term(pos, spans)]
        cut = min(ok, key=lambda pos: (abs(pos - target), -pos)) if ok else None
        if cut is not None and cut < target - 4:
            cut = max(ok)  # the only boundaries are far too early: take the latest one
        if cut is None:  # no word boundary: any position not inside a term or an ASCII run
            for pos in range(max_len, 0, -1):
                ascii_run = chars[pos - 1]["c"].isascii() and chars[pos]["c"].isascii()
                if not inside_term(pos, spans) and not ascii_run:
                    cut = pos
                    break
        cut = cut or max_len
        if len(chars) - cut <= 2 and len(chars) <= max_len + 2:
            break  # a 1-2 character tail stays with its line
        pieces.append(chars[:cut])
        chars = chars[cut:]
    if chars:
        pieces.append(chars)
    return pieces


def phrases(chars):
    out, cur = [], []
    for ch in chars:
        cur.append(ch)
        if ch["hard"]:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def wrap(text):
    """Two lines at most; break near the middle."""
    if len(text) <= MAX_LINE + 2:
        return text
    mid = (len(text) + 1) // 2
    return text[:mid] + "\\N" + text[mid:]


def segment_cues(chars, seg_start, seg_end, offset, terms):
    """Cues for one segment's characters, moved onto the output timeline."""
    chars = [c for c in chars if c["e"] > seg_start and c["s"] < seg_end]
    cues = []
    for ph in phrases(chars):
        for piece in split_phrase(ph, terms):
            a = max(piece[0]["s"], seg_start) - seg_start + offset
            b = min(piece[-1]["e"], seg_end) - seg_start + offset
            cues.append({"start": a, "end": b, "text": text_of(piece)})
    cues = merge_short(cues, offset, offset + (seg_end - seg_start))
    for c in cues:
        c["text"] = wrap(c["text"])
    return cues


def merge_short(cues, lo, hi):
    """Merge cues that would flash (too short or too few characters) into a neighbour, then
    close small gaps so captions don't flicker."""
    changed = True
    while changed and len(cues) > 1:
        changed = False
        for i, c in enumerate(cues):
            if c["end"] - c["start"] >= MIN_SECONDS and len(c["text"]) >= MIN_CHARS:
                continue
            prev_ok = i > 0 and len(cues[i - 1]["text"]) + len(c["text"]) <= MAX_CUE
            next_ok = i + 1 < len(cues) and len(cues[i + 1]["text"]) + len(c["text"]) <= MAX_CUE
            if not (prev_ok or next_ok):
                continue
            if prev_ok and (not next_ok or len(cues[i - 1]["text"]) <= len(cues[i + 1]["text"])):
                p = cues[i - 1]
                p["text"] += c["text"]
                p["end"] = c["end"]
            else:
                n = cues[i + 1]
                n["text"] = c["text"] + n["text"]
                n["start"] = c["start"]
            del cues[i]
            changed = True
            break
    for a, b in zip(cues, cues[1:]):
        if 0 < b["start"] - a["end"] < 0.3:
            a["end"] = b["start"]
    for c in cues:  # a lone short cue: give it reading time inside the segment
        if c["end"] - c["start"] < MIN_SECONDS:
            c["end"] = min(hi, c["start"] + MIN_SECONDS)
        c["start"], c["end"] = max(lo, c["start"]), min(hi, c["end"])
    return cues


# ---------- checks (used by QA) ----------

def cue_problems(cues, terms):
    problems = []
    for n, c in enumerate(cues, 1):
        plain = c["text"].replace("\\N", "")
        if c["end"] - c["start"] < MIN_SECONDS - 0.01:
            problems.append(f"字幕 {n}「{plain}」只显示 {c['end'] - c['start']:.2f} 秒")
        if len(plain) < MIN_CHARS - 1:
            problems.append(f"字幕 {n}「{plain}」只有 {len(plain)} 个字")
    for n, (a, b) in enumerate(zip(cues, cues[1:]), 1):
        ta, tb = a["text"].replace("\\N", ""), b["text"].replace("\\N", "")
        joined = ta + tb
        for term in terms:
            for m in re.finditer(re.escape(term), joined):
                if m.start() < len(ta) < m.end():
                    problems.append(f"字幕 {n}/{n + 1} 把「{term}」拆开了：「{ta}」|「{tb}」")
    return problems


# ---------- ASS output ----------

def ass_ts(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def srt_ts(t):
    t = max(0.0, t)
    h, rem = divmod(int(round(t * 1000)), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path, cues):
    path.write_text("".join(f"{n}\n{srt_ts(c['start'])} --> {srt_ts(c['end'])}\n{c['text'].replace(chr(92) + 'N', chr(10))}\n\n"
                            for n, c in enumerate(cues, 1)), encoding="utf-8")


def write_ass(path, cues, end_card=None, font="Microsoft YaHei", top_margin=300, bottom_margin=140):
    """cues: [{'start','end','text','pos': 'top'|'bottom'}]; end_card: {'start','end','lines': [...]}."""
    styles = [
        f"Style: Bottom,{font},{FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,0,2,60,60,{bottom_margin},1",
        f"Style: Top,{font},{FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,0,8,60,60,{top_margin},1",
        f"Style: CardTitle,{font},110,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,3,18,0,5,60,60,0,1",
        f"Style: CardLine,{font},68,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,3,14,0,5,60,60,0,1",
    ]
    events = []
    for c in cues:
        style = "Top" if c.get("pos") == "top" else "Bottom"
        events.append(f"Dialogue: 1,{ass_ts(c['start'])},{ass_ts(c['end'])},{style},,0,0,0,,{c['text']}")
    if end_card and end_card.get("lines"):
        y = 700
        for n, line in enumerate(end_card["lines"]):
            style = "CardTitle" if n == 0 else "CardLine"
            events.append(f"Dialogue: 2,{ass_ts(end_card['start'])},{ass_ts(end_card['end'])},{style},,0,0,0,,"
                          f"{{\\pos(540,{y})\\fad(200,0)}}{line}")
            y += 160 if n == 0 else 110
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n" + "\n".join(styles) + "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events) + "\n", encoding="utf-8-sig")
