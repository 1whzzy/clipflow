"""Delivered files (design §8). The platform keeps its own copy of each deliverable under its storage
root, records size and checksum, and serves downloads. Workflows only say which files they deliver.

Local folder now. Workers on two machines need shared or object storage (S3-compatible cloud storage);
that is another class with the same three methods.
"""
import hashlib
import mimetypes
import shutil
from pathlib import Path

from .contracts import ArtifactOut, BadOutput
from .tasks import new_id


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class LocalStorage:
    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def dir_for(self, task_id: str) -> Path:
        d = self.root / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def collect(self, task_id: str, outs: list[ArtifactOut]) -> list[dict]:
        """Check the workflow's files, copy any that live elsewhere into the task's folder, and return
        the rows to record. A missing required file raises `BadOutput`; a missing optional one is skipped."""
        rows, task_dir = [], self.dir_for(task_id)
        for out in outs:
            src = Path(out.path).resolve()
            if not src.is_file():
                if out.required:
                    raise BadOutput(f"required {out.kind} file not found: {src}")
                continue
            dst = src if src.is_relative_to(task_dir) else _free_name(task_dir / src.name)
            if dst != src:
                tmp = dst.with_name(dst.name + ".part")
                shutil.copyfile(src, tmp)
                tmp.replace(dst)  # a reader never sees a half-copied file
            rows.append({"id": new_id("art"), "kind": out.kind, "storage": self.name,
                         "location": dst.relative_to(self.root).as_posix(), "sha256": sha256_of(dst),
                         "bytes": dst.stat().st_size,
                         "mime": out.mime or mimetypes.guess_type(dst.name)[0] or "application/octet-stream"})
        return rows

    def path(self, location: str) -> Path:
        p = (self.root / location).resolve()
        if not p.is_relative_to(self.root):
            raise PermissionError(location)
        return p


def _free_name(p: Path) -> Path:
    n, candidate = 1, p
    while candidate.exists():
        n += 1
        candidate = p.with_name(f"{p.stem}-{n}{p.suffix}")
    return candidate
