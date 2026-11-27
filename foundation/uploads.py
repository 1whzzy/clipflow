"""Uploads: files people give a workflow, received in resumable chunks (design §15).

    POST /api/v1/uploads {name, bytes}        → {id, received: 0, chunk_bytes}
    PUT  /api/v1/uploads/{id}?offset=N  body  → {received, status}   (one chunk, raw bytes)
    GET  /api/v1/uploads/{id}                 → where it stands, to resume after a dropped connection

A chunk is accepted only at the offset the platform has reached (else 409 with that offset), so two
tabs or a retried request can't write the same bytes twice or leave a gap. The file is written as
`<name>.part` and renamed when the last byte arrives, after its checksum is taken: a workflow never
sees a half-received file. If the server stopped between writing a chunk and recording it, the
next chunk first cuts the file back to what was recorded.

A workflow's input names an upload with an `UploadRef` field (contracts.py); the platform refuses a
task whose upload isn't complete, and the workflow reads the file with `ctx.upload_path(id)`.
"""
import mimetypes
import re
from pathlib import Path

from .artifacts import LocalStorage, sha256_of
from .tasks import Conflict, NotFound, new_id

CHUNK_BYTES = 16 << 20  # what the page sends per request
MAX_CHUNK = 64 << 20  # what the server accepts per request
MAX_BYTES = 64 << 30  # 64 GB: a long replay is a few GB


class WrongOffset(Conflict):
    """The chunk isn't where the file has got to. `received` is where to continue from."""

    def __init__(self, message: str, received: int):
        super().__init__(message)
        self.received = received


def safe_name(name: str) -> str:
    """The last path part, without characters Windows or a shell would trip on."""
    base = re.split(r"[\\/]", name.strip())[-1]
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base).strip(" .")
    return base[:150] or "file"


class Uploads:
    def __init__(self, pool, storage: LocalStorage):
        self.pool, self.storage = pool, storage

    def create(self, name: str, size: int, mime: str | None = None, created_by: str = "") -> dict:
        if not 0 <= size <= MAX_BYTES:
            raise ValueError(f"file size must be between 0 and {MAX_BYTES} bytes")
        upload_id, name = new_id("up"), safe_name(name)
        location = f"uploads/{upload_id}/{name}"
        target = self.storage.path(location)
        target.parent.mkdir(parents=True, exist_ok=True)
        _part(target).write_bytes(b"")
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO uploads (id, name, bytes, mime, location, status, created_by)
                   VALUES (%s, %s, %s, %s, %s, 'receiving', %s) RETURNING *""",
                (upload_id, name, size, mime, location, created_by)).fetchone()
        return self._complete(upload_id) if size == 0 else row

    def get(self, upload_id: str) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id = %s", (upload_id,)).fetchone()
        if row is None:
            raise NotFound(upload_id)
        return row

    def append(self, upload_id: str, offset: int, data: bytes) -> dict:
        """Write one chunk at `offset`. Conflict (with the current offset) if that isn't where the file
        has got to; ValueError if the chunk runs past the declared size."""
        if len(data) > MAX_CHUNK:
            raise ValueError(f"a chunk may be at most {MAX_CHUNK} bytes")
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id = %s FOR UPDATE", (upload_id,)).fetchone()
            if row is None:
                raise NotFound(upload_id)
            if row["status"] == "complete":
                if offset == row["bytes"] and not data:
                    return row  # a repeated last request
                raise WrongOffset(f"upload {upload_id} is already complete", row["received"])
            if offset != row["received"]:
                raise WrongOffset(f"expected offset {row['received']}, got {offset}", row["received"])
            if offset + len(data) > row["bytes"]:
                raise ValueError(f"chunk runs past the declared size ({row['bytes']} bytes)")
            part = _part(self.storage.path(row["location"]))
            with part.open("r+b") as f:  # cut back anything written but never recorded, then append
                f.truncate(offset)
                f.seek(offset)
                f.write(data)
            received = offset + len(data)
            row = conn.execute("UPDATE uploads SET received = %s WHERE id = %s RETURNING *",
                               (received, upload_id)).fetchone()
        return self._complete(upload_id) if received == row["bytes"] else row

    def _complete(self, upload_id: str) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM uploads WHERE id = %s FOR UPDATE", (upload_id,)).fetchone()
            if row["status"] == "complete":
                return row
            target = self.storage.path(row["location"])
            part = _part(target)
            if part.stat().st_size != row["bytes"]:
                raise RuntimeError(f"upload {upload_id}: {part.stat().st_size} bytes on disk, {row['bytes']} expected")
            digest = sha256_of(part)
            part.replace(target)
            return conn.execute(
                """UPDATE uploads SET status = 'complete', sha256 = %s, completed_at = now()
                   WHERE id = %s RETURNING *""", (digest, upload_id)).fetchone()

    def path(self, upload_id: str) -> Path:
        """The complete file. NotFound for an unknown id; Conflict while it is still arriving."""
        row = self.get(upload_id)
        if row["status"] != "complete":
            raise Conflict(f"upload {upload_id} is not complete ({row['received']} of {row['bytes']} bytes)")
        return self.storage.path(row["location"])


def _part(target: Path) -> Path:
    return target.with_name(target.name + ".part")
