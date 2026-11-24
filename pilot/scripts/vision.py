"""Look at the picture: sample frames and have a vision model tag what the viewer can see.

The review of the first video said the clip talked about the pants without showing them (faces
and coats while the anchor talks about flare or fabric). So the agent needs to know, for every
moment it might use, what is on screen: is the product visible, which shot, which action.

Tags per frame (cached, so frames are only sent to the model once):
  shot     full_body / lower_body / upper_body / face_closeup / hem_shoes / fabric_detail /
           hanging / flat_lay / other
  pants    clear / partial / none        how well the product (the pants) can be seen
  hem      true if the hem (裤脚) is in frame
  action   standing / walking / turning / leg_lift / touching_fabric / showing_inside /
           holding_up / sitting / none
  color    the product's color as seen, e.g. 碳灰 / 黑色 / 看不清
  same     yes / no / unsure: is it the same item as the reference shots
  note     a few words

Standalone use (tags a product-shot folder and prints a summary):
  python pilot/scripts/vision.py --shots material/示例裤A/碳灰/ --product 示例裤A
"""
import argparse
import hashlib
import subprocess
from pathlib import Path

import common

SHOTS = ["full_body", "lower_body", "upper_body", "face_closeup", "hem_shoes", "fabric_detail",
         "hanging", "flat_lay", "other"]
ACTIONS = ["standing", "walking", "turning", "leg_lift", "touching_fabric", "showing_inside",
           "holding_up", "sitting", "none"]
SHOT_ZH = {"full_body": "全身", "lower_body": "下半身", "upper_body": "上半身", "face_closeup": "脸部近景",
           "hem_shoes": "裤脚鞋面", "fabric_detail": "面料特写", "hanging": "挂拍", "flat_lay": "平铺",
           "other": "其他"}
ACTION_ZH = {"standing": "站立", "walking": "走动", "turning": "转身", "leg_lift": "抬腿",
             "touching_fabric": "摸面料", "showing_inside": "翻内侧", "holding_up": "手拿展示",
             "sitting": "坐", "none": ""}
PANTS_ZH = {"clear": "裤子清楚", "partial": "裤子部分可见", "none": "看不到裤子"}

BATCH = 8           # frames per vision call
FRAME_HEIGHT = 480  # small frames are enough to judge shot and garment, and keep tokens low

TAG_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["frames"],
    "properties": {"frames": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["i", "shot", "pants", "hem", "action", "color", "same", "note"],
        "properties": {
            "i": {"type": "integer", "description": "图片序号，从 1 开始"},
            "shot": {"type": "string", "enum": SHOTS},
            "pants": {"type": "string", "enum": ["clear", "partial", "none"]},
            "hem": {"type": "boolean", "description": "裤脚是否在画面里"},
            "action": {"type": "string", "enum": ACTIONS},
            "color": {"type": "string", "description": "这条裤子的颜色，看不到写 看不清"},
            "same": {"type": "string", "enum": ["yes", "no", "unsure"],
                     "description": "和参考图是不是同一款"},
            "note": {"type": "string", "description": "十个字以内"}}}}},
}


# ---------- predicates used by the pick checks ----------

def shows_pants(t):
    """The viewer can see the legs and the hem of the pants being worn (the review: 看清裤腿和裤脚).
    A coat over the waist is fine as long as the legs and hem are in frame."""
    if t["shot"] not in ("full_body", "lower_body", "hem_shoes"):
        return False
    return t["pants"] == "clear" or (t["pants"] == "partial" and t["hem"])


def shows_cut(t):
    """The cut (版型) can be seen: worn, or a hanging / flat-lay shot with the whole pants clear
    (the review: 把挂拍调回版型段)."""
    return shows_pants(t) or (t["shot"] in ("hanging", "flat_lay") and t["pants"] == "clear")


def shows_hem_shoes(t):
    return t["hem"] and t["pants"] != "none" and (t["shot"] in ("hem_shoes", "full_body", "lower_body")
                                                    or t["action"] == "leg_lift")


def shows_fabric(t):
    return t["shot"] == "fabric_detail" or t["action"] in ("touching_fabric", "showing_inside")


# What the viewer must be able to see while the anchor talks about each kind of selling point.
POINT_NEEDS = {
    "版型": ("裤型（上身看得到裤腿和裤脚，或完整的挂拍/平铺）", shows_cut),
    "颜色": ("裤子颜色（上身看得到裤腿和裤脚，或完整的挂拍/平铺）", shows_cut),
    "搭配": ("裤脚和鞋面同框，或抬腿", shows_hem_shoes),
    "面料": ("面料细节（特写、摸面料或翻内侧）", shows_fabric),
}


def summary(tags):
    """One short Chinese description for a run of frames: the most common shot, pants, action."""
    if not tags:
        return "无画面信息"

    def most(key):
        values = [t[key] for t in tags]
        return max(set(values), key=values.count)

    parts = [SHOT_ZH[most("shot")], PANTS_ZH[most("pants")]]
    acts = sorted({ACTION_ZH[t["action"]] for t in tags if t["action"] != "none"})
    if acts:
        parts.append("/".join(acts))
    if any(t["hem"] for t in tags):
        parts.append("见裤脚")
    colors = [t["color"] for t in tags if t["color"] and t["color"] != "看不清"]
    if colors:
        parts.append(max(set(colors), key=colors.count))
    return "·".join(parts)


# ---------- frames ----------

def extract_frame(video, t, out):
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([common.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                    "-vf", f"scale=-2:{FRAME_HEIGHT}", "-q:v", "5", str(out)], check=True)
    return out


def grid_times(ranges, step):
    """Times on a fixed grid (multiples of step), so later runs reuse cached frames."""
    times = set()
    for a, b in ranges:
        t = (int(max(0.0, a) / step) + 1) * step
        while t <= b:
            times.add(round(t, 1))
            t += step
    return sorted(times)


class Tagger:
    def __init__(self, llm, product, references=()):
        self.llm, self.product, self.references = llm, product, list(references)[:2]

    def tag(self, frames, label):
        """frames: list of JPEG paths → list of tag dicts in the same order."""
        out = []
        for i in range(0, len(frames), BATCH):
            batch = frames[i:i + BATCH]
            ref = len(self.references)
            system = ("你在给带货短视频剪辑标注画面。只描述画面里看得到的东西，不要猜。"
                      "“裤子清楚”指观众能看清这条裤子的裤型（从腰到裤脚大部分可见、没被外套或桌子挡住）。")
            user = (f"商品：{self.product.get('name')}，{self.product.get('category', '')}，"
                    f"颜色 {'、'.join(self.product.get('colors') or [])}，"
                    f"卖点 {'、'.join(self.product.get('selling_points') or [])}。\n"
                    + (f"前 {ref} 张是这件商品的参考图（不用标注）。" if ref else "")
                    + f"后面 {len(batch)} 张需要标注，序号从 1 到 {len(batch)}。"
                    "逐张标注后调用 submit_tags。")

            def validate(args, n=len(batch)):
                got = sorted(f["i"] for f in args["frames"])
                return [] if got == list(range(1, n + 1)) else [f"需要正好标注 1–{n} 号图片各一次，收到 {got}"]

            args = self.llm.submit(system, user, "submit_tags", "提交每张图片的画面标注", TAG_SCHEMA,
                                   validate, label=f"{label} {i // BATCH + 1}",
                                   images=self.references + list(batch))
            by_i = {f["i"]: f for f in args["frames"]}
            out += [{k: v for k, v in by_i[n].items() if k != "i"} for n in range(1, len(batch) + 1)]
        return out


def cache_path(video):
    return common.ROOT / "pilot" / "work" / Path(video).stem / "vision.json"


def scan_video(tagger, video, ranges, step=2.0):
    """Tag frames every `step` seconds inside `ranges` of a replay. Returns {time: tags}.
    tagger=None: use the cache only (no model calls)."""
    path = cache_path(video)
    cache = common.load_json(path) if path.exists() else {"frames": {}}
    frames_dir = path.parent / "frames"
    todo = [t for t in grid_times(ranges, step) if f"{t:.1f}" not in cache["frames"]]
    if todo and tagger is None:
        print(f"  {Path(video).name}: {len(todo)} frames not tagged yet (offline: skipped)")
    elif todo:
        print(f"  {Path(video).name}: {len(todo)} new frames to look at")
        files = [extract_frame(video, t, frames_dir / f"t{t:07.1f}.jpg") for t in todo]
        for t, tags in zip(todo, tagger.tag(files, f"look {Path(video).stem}")):
            cache["frames"][f"{t:.1f}"] = tags
        cache["model"] = tagger.llm.model
        common.save_json(path, cache)
    wanted = set(f"{t:.1f}" for t in grid_times(ranges, step))
    return {float(k): v for k, v in cache["frames"].items() if k in wanted}


def frames_between(timeline, a, b):
    return [tags for t, tags in sorted(timeline.items()) if a <= t <= b]


def shot_key(f):
    st = f.stat()
    return hashlib.md5(f"{f.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]


def scan_shots(tagger, shots):
    """Tag three frames (10 %, 50 %, 90 %) of each product shot. Adds 'frames' and 'summary' to each.
    tagger=None: use the cache only (no model calls)."""
    if not shots:
        return shots
    folder = Path(shots[0]["file"]).parent
    path = common.ROOT / "pilot" / "work" / "_shots" / folder.name / "vision.json"
    cache = common.load_json(path) if path.exists() else {"shots": {}}
    todo, files = [], []
    for s in shots:
        key = shot_key(Path(s["file"]))
        s["key"] = key
        if key in cache["shots"] or tagger is None:
            continue
        for frac in (0.1, 0.5, 0.9):
            t = s["duration"] * frac
            todo.append((key, round(t, 2)))
            files.append(extract_frame(s["file"], t, path.parent / "frames" / f"{key}_{int(frac * 100):02d}.jpg"))
    if files:
        print(f"  product shots: {len(files)} new frames to look at")
        for (key, t), tags in zip(todo, tagger.tag(files, "look shots")):
            cache["shots"].setdefault(key, []).append({"t": t, **tags})
        cache["model"] = tagger.llm.model
        common.save_json(path, cache)
    for s in shots:
        s["frames"] = cache["shots"].get(s["key"], [])
        s["summary"] = summary(s["frames"])
    return shots


def reference_frames(shots, limit=2):
    """Middle frames of the first product shots, sent with every tagging call as 'this is the item'."""
    refs = []
    for s in shots[:limit]:
        folder = common.ROOT / "pilot" / "work" / "_shots" / Path(s["file"]).parent.name / "frames"
        refs.append(extract_frame(s["file"], s["duration"] * 0.5,
                                  folder / f"ref_{shot_key(Path(s['file']))}.jpg"))
    return refs


def list_shots(folder):
    if not folder:
        return []
    files = sorted(p for p in (common.ROOT / folder).glob("*") if p.suffix.lower() in {".mov", ".mp4"})
    shots = []
    for f in files:
        try:
            info = common.probe(f)
            v = next(s for s in info["streams"] if s["codec_type"] == "video")
            shots.append({"file": str(f), "duration": float(info["format"]["duration"]),
                          "width": v["width"], "height": v["height"]})
        except Exception as e:
            print(f"  skipped {f.name}: {e}")
    return shots


def main():
    import sys

    import yaml

    from llm import LLM, env_value
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shots", required=True, help="product-shot folder, relative to the repo root")
    p.add_argument("--product", required=True)
    p.add_argument("--model", help="vision model (default: LLM_VISION_MODEL, then LLM_MODEL)")
    args = p.parse_args()
    product = yaml.safe_load((common.ROOT / "products" / f"{args.product}.yaml").read_text(encoding="utf-8"))
    llm = LLM(args.model or env_value("LLM_VISION_MODEL"))
    shots = list_shots(args.shots)
    scan_shots(Tagger(llm, product, reference_frames(shots)), shots)
    for n, s in enumerate(shots, 1):
        print(f"B{n:<3} {Path(s['file']).name:32} {s['duration']:5.1f}s  {s['summary']}")
    print(f"LLM: {llm.usage()}")


if __name__ == "__main__":
    main()
