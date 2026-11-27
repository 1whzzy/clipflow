r"""Material intake (phase 4) as a platform workflow: one task per media file.

    replay  → transcribe → find where each product is sold → a person confirms the stretches → library
    footage → frames every 2 s → tag them → the stretches that clearly show the product → library

The work itself is Clipflow's (clipflow/ingest.py); this file adds the task around it: the question
to a person, the answer's checks, and publishing to the library (clipflow/library). Publishing
replaces what an earlier ingestion of the same file said about the same products, so a retry or a
re-scan is harmless. Its review state is the question itself: the proposal is in the waitpoint's
payload, and a retry recomputes it from the agents' cache without model calls.

Registering files: the workbench form, or the folder scanner, which submits a task for every new or
changed video file under a folder (a changed file = new size or modification time):

    .\.venv\Scripts\python.exe -m workflows.ingest scan material              # once
    .\.venv\Scripts\python.exe -m workflows.ingest scan material --every 600  # keep watching

Folder convention: `<folder>/<款名>/<颜色>/*.mov` is product footage of that product and colour (the
款名 must be in the catalog); any other video file is a replay, searched for every catalog product.
"""
import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from foundation.contracts import ArtifactOut, Finished, UploadRef, Waiting

ROOT = Path(__file__).resolve().parents[1]
VIDEO = {".mp4", ".mov", ".mkv", ".m4v", ".flv", ".ts"}


def asset_path(path: str) -> str:
    """How Clipflow names a file: relative to the repo root when inside it, else absolute; forward slashes."""
    p = Path(path)
    p = (p if p.is_absolute() else ROOT / p).resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


class IngestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["replay", "footage"] = Field(title="素材类型", description="replay 直播回放；footage 商品实拍素材")
    upload: UploadRef | None = Field(None, title="上传文件", description="从你的电脑选一个视频上传")
    path: str = Field("", max_length=1000, title="或者：服务器上的文件",
                      description="不上传时填：相对项目目录（如 material/11.10/xx.mp4）或完整路径")
    products: list[str] = Field([], max_length=200, title="商品（款名）",
                                description="回放：要找哪些款，留空=商品库里全部；实拍素材：拍的是哪一款，只填一个")
    color: str = Field("", max_length=40, title="颜色", description="实拍素材的颜色，可留空")
    confirm: bool = Field(True, title="入库前人工确认")

    @model_validator(mode="after")
    def _footage_is_one_product(self):
        if bool(self.upload) == bool(self.path.strip()):
            raise ValueError("请上传一个文件，或者填服务器上的文件路径（二选一）")
        if self.kind == "footage" and len(self.products) != 1:
            raise ValueError("footage needs exactly one product (款名)")
        return self


class SegmentEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: int | None = None  # the proposed segment this one keeps (quotes, tags); None = added by the reviewer
    product: str
    start: str | int  # "1:02:30" or ms
    end: str | int

    def ms(self) -> tuple[int, int]:
        from clipflow.contracts.request import parse_ms

        return parse_ms(self.start), parse_ms(self.end)


class IngestDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["approve", "edit", "reject"]
    version: int
    segments: list[SegmentEdit] | None = None  # edit: the whole corrected list
    reason: str = Field("", max_length=2000)

    @model_validator(mode="after")
    def _segments_only_for_edit(self):
        if (self.verdict == "edit") != (self.segments is not None):
            raise ValueError("segments is required for edit and only allowed with edit")
        return self

    @field_validator("segments")
    @classmethod
    def _times(cls, v):
        for n, s in enumerate(v or []):
            try:
                start, end = s.ms()
            except (ValueError, TypeError) as e:
                raise ValueError(f"segment {n + 1}: time not understood ({e})") from None
            if end <= start:
                raise ValueError(f"segment {n + 1}: end must be after start")
        return v


class IngestWorkflow:
    type = "ingest"
    version = 1
    label = "素材入库"
    input_model = IngestInput
    timeout_s = 4 * 3600.0  # transcribing an hour of replay takes ~8 min; locating every product, more

    @staticmethod
    def title(inp: dict) -> str:
        name = Path(inp["path"]).name if inp.get("path") else "上传的文件"
        return name + (f" · {'、'.join(inp['products'])}" if inp.get("products") else "")

    def __init__(self, engine_ctx=None, store=None, probe: Callable[[str], int] | None = None,
                 references: Callable[[str], list[str]] | None = None):
        """Without arguments, everything comes from .env on first use: the model gateway, the pilot's
        ASR and frames, and the library in Postgres."""
        self._ctx, self._store, self._probe, self._refs = engine_ctx, store, probe, references

    def _engine(self):
        if self._ctx is None or self._store is None:
            from clipflow.ingest import probe_ms
            from clipflow.library import LibraryStore, PostgresLibrary, library_schema
            from clipflow.run import real_context
            from clipflow.settings import Settings
            from clipflow.workers.pilot import PilotLibrary

            settings = Settings.load()
            if not settings.database_url:
                raise RuntimeError("DATABASE_URL missing from .env: the library lives in Postgres")
            self._store = self._store or LibraryStore.open(settings.database_url, library_schema())
            self._ctx = self._ctx or real_context(settings)
            self._probe = self._probe or probe_ms
            self._refs = self._refs or PostgresLibrary(self._store, PilotLibrary()).reference_images
        return self._ctx, self._store

    # ---- the platform calls these ------------------------------------------------------------

    def advance(self, task, command, ctx) -> Finished | Waiting:
        inp = IngestInput.model_validate(task.input)
        if command.kind == "resume":
            return self._resume(task, inp, ctx)
        engine, store = self._engine()
        path = asset_path(str(ctx.upload_path(inp.upload)) if inp.upload else inp.path)
        file = _file(path)
        products = []
        for name in inp.products or engine.catalog.ids():
            try:
                products.append(engine.catalog.get(name))
            except KeyError:
                raise ValueError(f"商品库里没有「{name}」") from None
            except ValidationError as e:  # an unfinished product sheet
                if inp.products:  # asked for by name: say so
                    first = e.errors()[0]
                    raise ValueError(f"「{name}」的商品资料不完整：{first['loc']} {first['msg']}") from None
                ctx.log("product_skipped", {"product": name, "reason": "商品资料不完整"})
        if not products:
            raise ValueError("商品库是空的：先在 products/ 里建商品资料")
        duration = self._probe(path)
        if inp.kind == "replay":
            segments, missing = _sell_blocks(engine, path, products, ctx)
        else:
            ctx.step("参考图")
            refs = (self._refs or (lambda p: []))(products[0].name)
            segments, missing = _footage(engine, path, products[0], inp.color or None, duration, refs, ctx), []
        proposal = {"asset": path, "kind": inp.kind, "duration_ms": duration, "bytes": file[0], "mtime": file[1],
                    "products": [p.name for p in products], "not_found": missing,
                    "color": inp.color or None, "segments": [s.model_dump(mode="json") for s in segments]}
        if inp.confirm and segments:
            asset_id = store.register(_asset(proposal), task.id)  # so the review page can play the file
            return self._ask({**proposal, "media_url": f"/api/v1/library/assets/{asset_id}/media"}, 1)
        return self._publish(task, ctx, store, proposal, by="")

    def check_decision(self, waitpoint, body: dict) -> str | None:
        try:
            d = IngestDecision.model_validate(body)
        except ValidationError as e:
            return "; ".join(f"{'.'.join(map(str, x['loc'])) or 'body'}: {x['msg']}" for x in e.errors())
        if d.verdict not in waitpoint.actions:
            return f"choose one of {waitpoint.actions}"
        products, proposed = waitpoint.payload.get("products", []), waitpoint.payload.get("segments", [])
        for n, s in enumerate(d.segments or [], 1):
            if s.product not in products:
                return f"第 {n} 段：商品「{s.product}」不在这次入库的商品里 {products}"
            if s.ref is not None and not 0 <= s.ref < len(proposed):
                return f"第 {n} 段：ref {s.ref} 不存在"
            if (duration := waitpoint.payload.get("duration_ms")) and s.ms()[1] > duration + 1000:
                return f"第 {n} 段：结束时间超出了文件长度"
        return None

    # ---- steps -----------------------------------------------------------------------------------

    def _ask(self, proposal: dict, version: int, error: str | None = None) -> Waiting:
        return Waiting(key="confirm_segments", subject_version=version,
                       payload={**proposal, "version": version, "error": error},
                       actions=["approve", "edit", "reject"], decision_schema=IngestDecision.model_json_schema(),
                       view="ingest.confirm_segments", engine_key=f"proposal-{version}", error=error)

    def _resume(self, task, inp, ctx) -> Finished | Waiting:
        wp, answer = task.resume.waitpoint, task.resume.decision
        d = IngestDecision.model_validate(answer.body)
        proposal = {k: v for k, v in wp.payload.items() if k not in ("version", "error")}
        version = wp.subject_version
        ctx.step(f"审核：{d.verdict}")
        if d.version != version:
            return self._ask(proposal, version,
                             error=f"版本不符：当前是 v{version}，提交的是 v{d.version}（请刷新后重新审核）")
        _, store = self._engine()
        if d.verdict == "reject":
            store.publish(_asset(proposal), [], proposal["products"], task.id, by=answer.by, status="rejected")
            return Finished(outcome="rejected_by_reviewer", summary={"reason": d.reason, "by": answer.by})
        if d.verdict == "edit":
            from clipflow.library import Segment

            proposed = proposal["segments"]
            segments = []
            for s in d.segments:  # a kept segment keeps its quotes and tags; an added one has none
                start, end = s.ms()
                base = dict(proposed[s.ref]) if s.ref is not None else {"kind": _kind(proposal)}
                segments.append(Segment.model_validate({**base, "product": s.product, "start_ms": start,
                                                        "end_ms": end}).model_dump(mode="json"))
            return self._ask({**proposal, "segments": sorted(segments, key=lambda x: (x["product"], x["start_ms"]))},
                             version + 1)
        return self._publish(task, ctx, store, proposal, by=answer.by, version=version)

    def _publish(self, task, ctx, store, proposal: dict, by: str, version: int = 0) -> Finished:
        from clipflow.library import Segment

        ctx.step("写入素材库")
        segments = [Segment.model_validate({**s, "confirmed": bool(by), "confirmed_by": by})
                    for s in proposal["segments"]]
        asset_id = store.publish(_asset(proposal), segments, proposal["products"], task.id, by=by)
        out = ctx.artifact_dir / "segments.json"
        tmp = out.with_suffix(".part")
        tmp.write_text(json.dumps({**proposal, "library_asset_id": asset_id, "confirmed_by": by},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(out)
        per_product = {p: sum(s.product == p for s in segments) for p in proposal["products"]}
        return Finished(outcome="delivered", artifacts=[ArtifactOut(kind="segments", path=out)],
                        summary={"asset": proposal["asset"], "segments": len(segments), "per_product": per_product,
                                 "not_found": proposal["not_found"], "confirmed_by": by, "version": version})


def _kind(proposal: dict) -> str:
    return "sell_block" if proposal["kind"] == "replay" else "shot"


def _asset(proposal: dict):
    from clipflow.library import AssetIn

    single = proposal["products"][0] if proposal["kind"] == "footage" else None
    return AssetIn(path=proposal["asset"], kind=proposal["kind"], bytes=proposal["bytes"], mtime=proposal["mtime"],
                   duration_ms=proposal["duration_ms"], product=single, color=proposal.get("color"))


def _file(path: str) -> tuple[int, float]:
    p = Path(path) if Path(path).is_absolute() else ROOT / path
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    st = p.stat()
    return st.st_size, st.st_mtime


def _sell_blocks(engine, path, products, ctx):
    from clipflow.ingest import sell_blocks

    return sell_blocks(engine, path, products, check=ctx.check, step=ctx.step)


def _footage(engine, path, product, color, duration, refs, ctx):
    from clipflow.ingest import footage_shots

    return footage_shots(engine, path, product, color, duration, refs, check=ctx.check, step=ctx.step)


# ---- the folder scanner ----------------------------------------------------------------------------

def plan_scan(folder: Path, products: set[str]) -> list[tuple[dict, str]]:
    """(task input, client_request_id) for every video file under `folder`. The id names the file's
    size and modification time, so submitting the same file again creates no new task."""
    out = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in VIDEO:
            continue
        parts = f.relative_to(folder).parts
        path = asset_path(str(f))
        if len(parts) >= 3 and parts[0] in products:
            inp = IngestInput(kind="footage", path=path, products=[parts[0]], color=parts[1], confirm=False)
        else:
            inp = IngestInput(kind="replay", path=path)
        st = f.stat()
        out.append((inp.model_dump(mode="json"), f"ingest:{path}:{st.st_size}:{int(st.st_mtime)}"))
    return out


def scan(folder: Path, repo, products: set[str], by: str = "scanner") -> list[str]:
    """Submit an ingest task for every new or changed file. Returns the ids of the tasks it created."""
    created = []
    for inp, key in plan_scan(folder, products):
        row, new = repo.submit("ingest", IngestWorkflow.version, inp, created_by=by, client_request_id=key)
        if new:
            created.append(row["id"])
    return created


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m workflows.ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="submit an ingest task for every new or changed video file")
    sc.add_argument("folder", nargs="?", default="material")
    sc.add_argument("--every", type=float, default=0, help="seconds between scans; 0 = scan once")
    sc.add_argument("--dry-run", action="store_true", help="list what would be submitted")
    args = ap.parse_args(argv)

    from clipflow.workers.pilot import YamlCatalog

    folder = Path(args.folder) if Path(args.folder).is_absolute() else ROOT / args.folder
    if not folder.is_dir():
        print(f"no such folder: {folder}")
        return 2
    products = set(YamlCatalog().ids())
    if args.dry_run:
        for inp, _ in plan_scan(folder, products):
            print(f"{inp['kind']:<8} {inp['path']}  {','.join(inp['products'])} {inp['color']}")
        return 0

    from foundation import db
    from foundation.dispatch.postgres import PostgresDispatcher
    from foundation.settings import PlatformSettings
    from foundation.tasks import TaskRepository

    s = PlatformSettings.load()
    pool = db.open_pool(s.database_url, s.schema)
    db.check(pool)
    repo = TaskRepository(pool, PostgresDispatcher(s.database_url, s.schema))
    while True:
        created = scan(folder, repo, products)
        listed = f": {', '.join(created)}" if created else ""
        print(f"{time.strftime('%H:%M:%S')} {len(created)} new ingest task(s){listed}")
        if not args.every:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
