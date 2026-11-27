"""The material library's API for the workbench's 素材库 page, served at /api/v1/library/ by the
platform (foundation/registry.py MODULES).

    GET /products                 products with something in the library (plus catalog products without)
    GET /segments?product=…       recall: a product's segments, each with its file and time range
    GET /assets/{id}/media        the file itself, with Range support (the page seeks to a segment)
    GET /segments/{id}/frame      a frame ingestion tagged in a footage segment (HEVC footage may not
                                  play in every browser; the frame always shows)

Files are served only by their library id, never by a path from the request.
"""
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..settings import ROOT
from .store import LibraryStore

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")


def _file(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def router(store: LibraryStore | None = None, catalog=None) -> APIRouter:
    if store is None:
        from ..settings import Settings
        from . import library_schema

        store = LibraryStore.open(Settings.load().database_url, library_schema())
    if catalog is None:
        from ..workers.pilot import YamlCatalog

        catalog = YamlCatalog()
    r = APIRouter(tags=["library"])

    @r.get("/products")
    def products():
        known = {p["product"]: p for p in store.products()}
        for name in catalog.ids():
            known.setdefault(name, {"product": name, "sell_blocks": 0, "shots": 0, "files": 0})
        return sorted(known.values(), key=lambda p: p["product"])

    @r.get("/segments")
    def segments(product: str = Query(min_length=1), kind: str | None = None, limit: int = Query(500, le=2000)):
        rows = store.segments(product, kind=kind, limit=limit)
        return [{"id": s["id"], "product": s["product"], "kind": s["kind"], "path": s["path"],
                 "start_ms": s["start_ms"], "end_ms": s["end_ms"], "shot_type": s["shot_type"], "color": s["color"],
                 "quality": s["quality"], "confidence": s["confidence"], "evidence": s["evidence"],
                 "text": s["text"][:300], "confirmed": s["confirmed"], "confirmed_by": s["confirmed_by"],
                 "task_id": s["task_id"], "media_url": f"/api/v1/library/assets/{s['asset_id']}/media",
                 "frame_url": f"/api/v1/library/segments/{s['id']}/frame" if s["frames"] else None}
                for s in rows]

    @r.get("/assets/{asset_id}/media")
    def media(asset_id: str):
        a = store.asset(asset_id)
        if a is None:
            raise HTTPException(404, f"no asset {asset_id}")
        path = _file(a["path"])
        if not path.is_file():
            raise HTTPException(410, f"asset {asset_id} is in the library but its file is gone: {a['path']}")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @r.get("/segments/{segment_id}/frame")
    def frame(segment_id: str):
        s = store.segment(segment_id)
        if s is None:
            raise HTTPException(404, f"no segment {segment_id}")
        frames = sorted(s["frames"], key=lambda f: f.get("product_visible") != "clear")
        for f in frames:
            path = _file(f.get("path", ""))
            if path.is_file():
                return FileResponse(path, media_type="image/jpeg")
        raise HTTPException(404, "no frame on disk for this segment")

    return r
