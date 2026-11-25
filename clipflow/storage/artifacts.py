"""Artifact store: where big objects live. Graph state only holds their ids.

An artifact id is `<kind>/<hash of its JSON>`, so the same content always gets the same id: storing
twice is free, and ids double as cache keys. Artifacts are never changed in place; a new version is
a new artifact (V2.0 tech plan §6.3, state vs. deliverables).
"""
import hashlib
import json
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


def content_hash(data) -> str:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class ArtifactStore(Protocol):
    def put(self, kind: str, obj: BaseModel) -> str: ...

    def get(self, artifact_id: str, model: type[M]) -> M: ...

    def get_cached(self, key: str) -> dict | None:
        """Cached agent/worker output by input key, or None."""
        ...

    def put_cached(self, key: str, data: dict) -> None: ...


class MemoryStore:
    """For tests: everything in a dict."""

    def __init__(self):
        self._items: dict[str, str] = {}
        self._cache: dict[str, dict] = {}

    def put(self, kind: str, obj: BaseModel) -> str:
        artifact_id = f"{kind}/{content_hash(obj)}"
        self._items[artifact_id] = obj.model_dump_json()
        return artifact_id

    def get(self, artifact_id: str, model: type[M]) -> M:
        return model.model_validate_json(self._items[artifact_id])

    def get_cached(self, key: str) -> dict | None:
        return self._cache.get(key)

    def put_cached(self, key: str, data: dict) -> None:
        self._cache[key] = data


class FileStore:
    """On disk: <root>/<kind>/<hash>.json and <root>/_cache/<key>.json. Local disk for the pilot;
    the interface is what an object store (S3-compatible) would implement later."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, kind: str, obj: BaseModel) -> str:
        artifact_id = f"{kind}/{content_hash(obj)}"
        path = self.root / f"{artifact_id}.json"
        if not path.exists():
            _write_atomic(path, obj.model_dump_json(indent=2))
        return artifact_id

    def get(self, artifact_id: str, model: type[M]) -> M:
        return model.model_validate_json((self.root / f"{artifact_id}.json").read_text(encoding="utf-8"))

    def get_cached(self, key: str) -> dict | None:
        path = self.root / "_cache" / f"{key}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def put_cached(self, key: str, data: dict) -> None:
        _write_atomic(self.root / "_cache" / f"{key}.json", json.dumps(data, ensure_ascii=False))


def _write_atomic(path: Path, text: str) -> None:
    """Write to a temporary file next to the target, then rename it into place. Another run (or a
    parallel agent call) reading the same file sees the old file or the whole new one, never half."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
