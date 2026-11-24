"""Real workers for the pilot, on this workstation: adapters over the prototype in pilot/scripts/.

Asset ids are file paths relative to the repo root (e.g. "material/11.10/26_11_10 11_27 (2).mp4").
That is enough for one workstation; a media table with stable ids replaces it later.

What each adapter reuses from the prototype:
- ASR: transcribe.py via run_job.ensure_transcript / ensure_words (cached in pilot/work/<video>/).
- Frames: vision.extract_frame on the same 2-second grid, so cached frames are reused.
- Library + references: the prototype's tags of the product shots (pilot/work/_shots/<folder>/vision.json)
  stand in for what ingestion will provide.
- Renderer: ffmpeg directly from the Timeline (per-shot parts cached → partial re-render), captions
  as ASS via cues.write_ass, loudness normalised to −14 LUFS.
"""
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..agents.vision import Frame
from ..contracts import (
    AssetRange, Line, Measured, Product, ProductBlocks, RenderResult, SourceVideo, TimeRange, Timeline, Transcript,
    Word,
)
from ..settings import ROOT

PILOT = ROOT / "pilot" / "scripts"
if str(PILOT) not in sys.path:
    sys.path.insert(0, str(PILOT))

import common  # noqa: E402  (pilot/scripts)

W, H, FPS = 1080, 1920, 30


def path_of(asset_id: str) -> Path:
    p = ROOT / asset_id
    if not p.exists():
        raise FileNotFoundError(f"asset {asset_id!r} not found at {p}")
    return p


def _ms(seconds: float) -> int:
    return int(round(float(seconds) * 1000))


# ---- ASR ----------------------------------------------------------------------------------------

@dataclass
class PilotASR:
    def transcribe(self, asset_id: str, window: TimeRange | None = None) -> Transcript:
        import run_job

        rows = run_job.ensure_transcript(path_of(asset_id))
        lines = [Line(i=n, start_ms=_ms(r["start"]), end_ms=max(_ms(r["end"]), _ms(r["start"]) + 1), text=r["text"])
                 for n, r in enumerate(rows) if r["text"].strip()]
        if window:
            lines = [l for l in lines if l.end_ms > window.start_ms and l.start_ms < window.end_ms]
        return Transcript(asset_id=asset_id, range=window, model="faster-whisper large-v3", lines=lines)

    def clauses(self, asset_id: str, ranges: list[TimeRange]) -> Transcript:
        import run_job

        spans = run_job.merge_ranges([(r.start_ms / 1000, r.end_ms / 1000) for r in ranges], gap=0)
        clauses = run_job.ensure_words(path_of(asset_id), spans)
        inside = [c for c in clauses if any(a - 0.5 <= c["start"] and c["end"] <= b + 0.5 for a, b in spans)]
        lines = []
        for n, c in enumerate(inside):
            words = [Word(w=w["word"], start_ms=_ms(w["start"]), end_ms=_ms(w["end"])) for w in c["words"] or []]
            lines.append(Line(i=n, start_ms=_ms(c["start"]), end_ms=max(_ms(c["end"]), _ms(c["start"]) + 1),
                              text=c["text"], words=words or None))
        return Transcript(asset_id=asset_id, model="faster-whisper large-v3 (words)", lines=lines)


# ---- frames ---------------------------------------------------------------------------------------

@dataclass
class PilotFrames:
    def sample(self, asset_id: str, ranges: list[TimeRange], every_ms: int) -> list[Frame]:
        import vision

        video = path_of(asset_id)
        folder = ROOT / "pilot" / "work" / video.stem / "frames"
        times = vision.grid_times([(r.start_ms / 1000, r.end_ms / 1000) for r in ranges], every_ms / 1000)
        frames = []
        for n, t in enumerate(times):
            out = vision.extract_frame(video, t, folder / f"t{t:07.1f}.jpg")
            frames.append(Frame(index=n, asset_id=asset_id, at_ms=_ms(t), path=str(out)))
        return frames


# ---- catalog and library ------------------------------------------------------------------------

def _product_yaml(name: str) -> dict:
    path = ROOT / "products" / f"{name}.yaml"
    if not path.exists():
        raise KeyError(name)
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class YamlCatalog:
    """products/<款名>.yaml → Product."""

    def get(self, product_id: str) -> Product:
        p = _product_yaml(product_id)
        return Product(**{k: p.get(k) for k in Product.model_fields if p.get(k) is not None})


# prototype shot tags → contract shot types
SHOT_MAP = {"full_body": "look_full", "lower_body": "detail_fit", "upper_body": "look_half",
            "face_closeup": "talking_head", "hem_shoes": "detail_fit", "fabric_detail": "detail_fabric",
            "hanging": "hanging", "flat_lay": "flat_lay", "other": "other"}


def _same_color(a: str, b: str) -> bool:
    norm = lambda s: s.replace("炭", "碳").replace("色", "")  # noqa: E731  炭灰色 (folder) = 碳灰 (sheet)
    return norm(a) == norm(b)


@dataclass
class PilotLibrary:
    """Product footage from products/<款名>.yaml, tagged by the prototype (stand-in for ingestion)."""

    _shots: dict = field(default_factory=dict)

    def replays_for(self, product: str) -> list[SourceVideo]:
        return [SourceVideo(asset_id=r["file"]) for r in _product_yaml(product).get("replays", [])]

    def linked_blocks(self, product: str, asset_id: str) -> ProductBlocks | None:
        return None  # ingestion doesn't attribute replays yet: locate_product does it

    def shots(self, product: str) -> list[dict]:
        if product not in self._shots:
            import vision

            out = []
            for folder in _product_yaml(product).get("product_shots", []):
                color = Path(folder.rstrip("/")).name
                cache = ROOT / "pilot" / "work" / "_shots" / color / "vision.json"
                tags = json.loads(cache.read_text(encoding="utf-8"))["shots"] if cache.exists() else {}
                for s in vision.list_shots(folder):
                    key = vision.shot_key(Path(s["file"]))
                    frames = tags.get(key, [])
                    usable = [f for f in frames if f.get("pants") != "none" and f.get("same") != "no"]
                    if not usable:
                        continue
                    shot = max(set(f["shot"] for f in usable), key=[f["shot"] for f in usable].count)
                    out.append({"asset_id": str(Path(s["file"]).relative_to(ROOT)).replace("\\", "/"),
                                "duration_ms": _ms(s["duration"]), "shot_type": SHOT_MAP.get(shot, "other"),
                                "color": color, "clear": sum(f.get("pants") == "clear" for f in usable)})
            self._shots[product] = sorted(out, key=lambda s: -s["clear"])
        return self._shots[product]

    def find(self, product, colors, shot_type, min_ms):
        out = []
        for s in self.shots(product):
            if shot_type and s["shot_type"] != shot_type:
                continue
            if colors and not any(_same_color(s["color"], c) for c in colors):
                continue
            if s["duration_ms"] < min(min_ms, 2000):
                continue
            color = next((c for c in colors if _same_color(s["color"], c)), s["color"])
            start = min(1000, max(0, s["duration_ms"] - min_ms))  # skip the first second (camera settling)
            out.append((s["asset_id"], AssetRange(start_ms=start, end_ms=s["duration_ms"], shot_type=s["shot_type"],
                                                  color=color, confirmed=False)))
        return out

    def reference_images(self, product: str) -> list[str]:
        refs = []
        for folder in _product_yaml(product).get("product_shots", []):
            refs += sorted(str(p) for p in (ROOT / "pilot" / "work" / "_shots" / Path(folder.rstrip("/")).name
                                            / "frames").glob("ref_*.jpg"))
        return refs[:2]


class NoCases:
    """The case library isn't wired yet (open question for the architect: what to store, how to query)."""

    def similar(self, category, price, focus=(), hook_style="any", k=3):
        return []


# ---- rendering ------------------------------------------------------------------------------------

def _run(cmd: list, cwd=None) -> None:
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({r.returncode}): {r.stderr[-800:]}")


def _wrap(text: str, width: int = 14) -> str:
    """Two caption lines at most, split near the middle (ASS line break: \\N)."""
    text = text.strip("，,。 ")
    if len(text) <= width:
        return text
    cut = len(text) // 2
    for d in range(0, cut):
        for k in (cut - d, cut + d):
            if 0 < k < len(text) and text[k - 1] in "，,。！？ ":
                return text[:k].strip("，, ") + "\\N" + text[k:].strip("，, ")
    return text[:cut] + "\\N" + text[cut:]


@dataclass
class PilotRenderer:
    out_root: Path = ROOT / "renders"
    cache: Path = ROOT / "pilot" / "work" / "parts"

    def _part(self, item, audio, enc) -> tuple[Path, bool]:
        """One shot as a 1080x1920/30fps clip with its audio. Cached by content → partial re-render."""
        key = hashlib.sha256(json.dumps([item.model_dump(exclude={"at_ms"}),
                                         audio.model_dump(exclude={"at_ms"}) if audio else None],
                                        sort_keys=True).encode()).hexdigest()[:16]
        out = self.cache / f"{key}.mp4"
        if out.exists():
            return out, True
        out.parent.mkdir(parents=True, exist_ok=True)
        dur = (item.out_ms - item.in_ms) / 1000
        cmd = [common.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
        if item.asset_id == "placeholder:missing":
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"color=c=gray:s={W}x{H}:r={FPS}"]
        else:
            cmd += ["-ss", f"{item.in_ms / 1000:.3f}", "-t", f"{dur:.3f}", "-i", str(path_of(item.asset_id))]
        if audio:
            cmd += ["-ss", f"{audio.in_ms / 1000:.3f}", "-t", f"{dur:.3f}", "-i", str(path_of(audio.asset_id))]
        else:
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
              f"format=yuv420p,setsar=1[v]")
        af = f"[1:a]afade=t=in:d=0.04,afade=t=out:st={max(0.0, dur - 0.06):.3f}:d=0.06,aresample=48000,apad[a]"
        _run(cmd + ["-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", *enc,
                    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(out)])
        return out, False

    def render(self, timeline: Timeline, previous: RenderResult | None = None) -> RenderResult:
        import cues as cuelib

        enc = common.video_encoder_args()
        audio_at = {a.at_ms: a for a in timeline.audio}
        parts, reused = [], []
        for item in timeline.video:
            part, hit = self._part(item, audio_at.get(item.at_ms), enc)
            parts.append(part)
            if hit:
                reused.append(item.shot_seq)
        out_dir = self.out_root / timeline.run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"final_v{timeline.version}.mp4"
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / "list.txt"
            listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
            cut = Path(tmp) / "cut.mp4"
            _run([common.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                  "-i", listing, "-c", "copy", cut])
            cue_list = [{"start": c.start_ms / 1000, "end": c.end_ms / 1000, "text": _wrap(c.text),
                         "pos": c.position} for c in timeline.captions]
            card = timeline.end_card
            cuelib.write_ass(Path(tmp) / "captions.ass", cue_list,
                             {"start": card.start_ms / 1000, "end": card.end_ms / 1000, "lines": card.lines} if card else None)
            hold = (card.end_ms - card.start_ms) / 1000 if card else 0
            vf = f"tpad=stop_mode=clone:stop_duration={hold},subtitles=captions.ass"
            af = f"apad=pad_dur={hold},loudnorm=I=-14:TP=-1.5:LRA=11"
            _run([common.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", "cut.mp4", "-vf", vf, *enc,
                  "-af", af, "-ar", "48000", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final)],
                 cwd=tmp)
        return RenderResult(run_id=timeline.run_id, timeline_version=timeline.version,
                            path=str(final.relative_to(ROOT)).replace("\\", "/"), reused_shots=reused)

    def measure(self, path: str) -> Measured:
        p = ROOT / path
        info = common.probe(p)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        num, den = (int(x) for x in v["r_frame_rate"].split("/"))
        r = subprocess.run([common.ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(p), "-af",
                            "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
        stats = json.loads(m.group(0)) if m else {}
        return Measured(duration_ms=_ms(info["format"]["duration"]), fps=num / den, width=v["width"],
                        height=v["height"], lufs=float(stats["input_i"]) if "input_i" in stats else None,
                        peak_db=float(stats["input_tp"]) if "input_tp" in stats else None)


@dataclass
class NoTTS:
    def speak(self, run_id, lines, voice):
        raise NotImplementedError("no TTS service chosen yet (material mode); open question")
