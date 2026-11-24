"""Transcribe a video (or time windows of it) to a timestamped Chinese transcript.

Outputs, next to --out (default: pilot/work/<video name>/):
  <name>.json  segments with absolute source timestamps (seconds); with --words, each segment
               also has word timestamps
  <name>.txt   one line per segment, "[hh:mm:ss.mmm-hh:mm:ss.mmm] text", easy to read and search

Examples:
  python transcribe.py D:/footage/2026-11-15.mp4 --start 1:10:00 --end 1:50:00
  python transcribe.py replay.mp4 --windows 690-950,1280-1520 --words --name words
"""
import argparse
import re
import tempfile
import time
from pathlib import Path

import common


def extract_audio(video, start, end, wav_path):
    cmd = [common.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", str(start)]
    if end:
        cmd += ["-to", str(end)]
    cmd += ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)]
    # -ss/-to before -i seek on the input, so times stay absolute to the source file
    common.run(cmd)


def load_wav(path):
    """Read the 16 kHz mono PCM WAV from extract_audio as float32 in [-1, 1].
    Passing an array avoids faster-whisper's PyAV decoder, which breaks with PyAV 19."""
    import wave

    import numpy as np
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


PROMPT = "以下是直播带货的普通话内容，请使用简体中文。"


def is_prompt_echo(text):
    rest = text
    for p in ("请使用简体中文", "以下是直播带货的普通话内容"):
        rest = rest.replace(p, "")
    return not re.sub(r"[\s，。！？、,.!?;；:：]", "", rest)


def parse_windows(text):
    out = []
    for part in text.split(","):
        a, b = part.split("-")
        out.append((float(a), float(b)))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video")
    p.add_argument("--start", help="window start (seconds or hh:mm:ss)")
    p.add_argument("--end", help="window end (seconds or hh:mm:ss)")
    p.add_argument("--windows", help="several windows in seconds, 'a-b,c-d' (one model load for all)")
    p.add_argument("--model", default="large-v3")
    p.add_argument("--language", default="zh")
    p.add_argument("--out", help="output folder")
    p.add_argument("--words", action="store_true",
                   help="also keep word timestamps (needed for exact cuts and caption timing)")
    p.add_argument("--name", default="transcript", help="output file name without extension")
    args = p.parse_args()

    video = Path(args.video)
    if args.windows:
        windows = parse_windows(args.windows)
    else:
        windows = [(common.parse_ts(args.start) or 0.0, common.parse_ts(args.end))]
    out_dir = Path(args.out) if args.out else common.ROOT / "pilot" / "work" / video.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    common.add_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    t0 = time.time()
    model = WhisperModel(common.resolve_whisper_model(args.model), device="cuda", compute_type="float16",
                         download_root=str(common.ROOT / "models"))
    rows, total_audio = [], 0.0
    with tempfile.TemporaryDirectory() as tmp:
        for n, (start, end) in enumerate(windows):
            wav = Path(tmp) / f"audio{n}.wav"
            extract_audio(video, start, end, wav)
            segments, info = model.transcribe(
                load_wav(wav), language=args.language, vad_filter=True, beam_size=5,
                initial_prompt="以下是直播带货的普通话内容，请使用简体中文。",
                word_timestamps=args.words,
            )
            for seg in segments:
                text = seg.text.strip()
                if is_prompt_echo(text):  # Whisper repeating its prompt over music or silence
                    continue
                row = {"start": round(seg.start + start, 2), "end": round(seg.end + start, 2), "text": text}
                if args.words and seg.words:
                    row["words"] = [{"start": round(w.start + start, 2), "end": round(w.end + start, 2),
                                     "word": w.word} for w in seg.words]
                rows.append(row)
                print(f"[{common.fmt_ts(row['start'])}] {row['text']}", flush=True)
            total_audio += info.duration

    common.save_json(out_dir / f"{args.name}.json", {
        "source": str(video.resolve()), "windows": windows, "model": args.model,
        "duration": total_audio, "words": args.words, "segments": rows,
    })
    lines = [f"[{common.fmt_ts(r['start'])}-{common.fmt_ts(r['end'])}] {r['text']}" for r in rows]
    (out_dir / f"{args.name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(rows)} segments, {total_audio:.0f}s of audio in {time.time() - t0:.0f}s")
    print(f"wrote {out_dir / (args.name + '.json')}")


if __name__ == "__main__":
    main()
