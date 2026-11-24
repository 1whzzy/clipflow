"""Shared helpers for the pilot scripts."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def add_cuda_dll_dirs():
    """On Windows, make the pip-installed NVIDIA DLLs (cuBLAS, cuDNN) findable."""
    if os.name != "nt":
        return
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not site.exists():
        return
    for bin_dir in site.glob("*/bin"):
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def resolve_whisper_model(name):
    """Prefer a hand-downloaded folder models/faster-whisper-<name>/ (with model.bin);
    otherwise return the name so faster-whisper downloads it from Hugging Face."""
    if Path(name).is_dir():
        return str(Path(name))
    local = ROOT / "models" / f"faster-whisper-{name}"
    if (local / "model.bin").exists():
        return str(local)
    return name


def ffmpeg_bin(name="ffmpeg"):
    found = shutil.which(name)
    if found:
        return found
    # winget installs a shim here; a shell opened before the install may not have it on PATH
    shim = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
    if shim.exists():
        return str(shim)
    sys.exit(f"{name} not found. Install it with: winget install Gyan.FFmpeg")


def run(cmd, cwd=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    result = subprocess.run([str(c) for c in cmd], cwd=cwd)
    if result.returncode != 0:
        sys.exit(f"command failed with exit code {result.returncode}")


def probe(path):
    out = subprocess.run(
        [ffmpeg_bin("ffprobe"), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout  # ffprobe prints UTF-8; without this, Windows decodes it as GBK and fails
    return json.loads(out)


def has_nvenc():
    out = subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    return "h264_nvenc" in out


def video_encoder_args():
    if has_nvenc():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]


def fmt_ts(seconds):
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def parse_ts(value):
    """Accept seconds ('754.2') or a clock time ('1:02:34' / '12:34.5')."""
    if value is None:
        return None
    value = str(value).strip()
    if ":" not in value:
        return float(value)
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
