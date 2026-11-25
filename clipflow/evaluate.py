r"""Evaluation against a golden set: how close the agents come to what an editor chose.

The spec's pilot targets (8.2): `locate_product` finds the product in ≥ 90 % of cases, and the
chosen footage overlaps the human cut by ≥ 80 %. A golden case is one replay + one product + the
time ranges an editor actually used (from the human-cut slices on the shared drive; mapping a
slice back to replay time is done once, by hand or by matching transcripts).

    .\.venv\Scripts\python.exe -m clipflow.evaluate golden.yaml        # scores the latest stored runs

golden.yaml:
    cases:
      - product: 示例裤A
        asset_id: material/demo/live_part2.mp4
        human: [["21:40", "21:46"], ["4:16", "4:23"]]   # what the editor used (m:ss or ms)
"""
import sys
from dataclasses import dataclass

from .contracts import Plan, ProductBlocks, TimeRange
from .contracts.request import parse_ms

FOUND_TARGET = 0.90
OVERLAP_TARGET = 0.80


@dataclass(frozen=True)
class Case:
    product: str
    asset_id: str
    human: list[TimeRange]


def _merge(ranges: list[TimeRange]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for r in sorted(ranges, key=lambda r: r.start_ms):
        if out and r.start_ms <= out[-1][1]:
            out[-1][1] = max(out[-1][1], r.end_ms)
        else:
            out.append([r.start_ms, r.end_ms])
    return [(a, b) for a, b in out]


def covered_ms(target: TimeRange, by: list[TimeRange]) -> int:
    """How much of `target` lies inside the union of `by`."""
    return sum(max(0, min(b, target.end_ms) - max(a, target.start_ms)) for a, b in _merge(by))


def locate_scores(blocks: ProductBlocks, human: list[TimeRange], min_cover: float = 0.5) -> dict:
    """found: share of the editor's ranges that the located blocks cover at least `min_cover` of.
    precision: share of located time that the editor used (low = blocks much wider than needed)."""
    located = [TimeRange(start_ms=b.start_ms, end_ms=b.end_ms) for b in blocks.blocks]
    found = sum(covered_ms(h, located) >= min_cover * h.duration_ms for h in human)
    located_ms = sum(b - a for a, b in _merge(located))
    used_ms = sum(covered_ms(h, located) for h in [TimeRange(start_ms=a, end_ms=b) for a, b in _merge(human)])
    return {"found": found / len(human) if human else 1.0,
            "precision": used_ms / located_ms if located_ms else 0.0}


def overlap_score(plan: Plan, human: list[TimeRange]) -> float:
    """Share of the video's live footage (seconds of the replay it uses) that the editor also used."""
    picked = [TimeRange(start_ms=s.speech.in_ms, end_ms=s.speech.out_ms) for s in plan.shots
              if s.speech and s.speech.kind == "live"]
    total = sum(r.duration_ms for r in picked)
    return sum(covered_ms(r, human) for r in picked) / total if total else 0.0


def summary(rows: list[dict]) -> dict:
    """Pilot gate over all cases: mean scores and pass/fail against the spec's targets."""
    n = len(rows) or 1
    found = sum(r["found"] for r in rows) / n
    overlap = sum(r["overlap"] for r in rows if r.get("overlap") is not None) / max(
        1, sum(r.get("overlap") is not None for r in rows))
    return {"cases": len(rows), "found": found, "overlap": overlap,
            "passed": found >= FOUND_TARGET and overlap >= OVERLAP_TARGET}


def load_golden(path: str) -> list[Case]:
    import yaml

    from pathlib import Path

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [Case(product=c["product"], asset_id=c["asset_id"],
                 human=[TimeRange(start_ms=parse_ms(a), end_ms=parse_ms(b)) for a, b in c["human"]])
            for c in data["cases"]]


def main(argv=None) -> int:  # pragma: no cover - reads stored runs
    """Score the newest stored blocks and plan per golden case (from runs already made)."""
    import json
    from pathlib import Path

    from .settings import Settings

    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    root = Path(Settings.load().artifacts_dir)

    def newest(kind, model, keep):
        files = sorted((root / kind).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            obj = model.model_validate_json(f.read_text(encoding="utf-8"))
            if keep(obj):
                return obj
        return None

    rows = []
    for case in load_golden(argv[0]):
        blocks = newest("blocks", ProductBlocks, lambda b, c=case: b.product == c.product and b.asset_id == c.asset_id)
        plan = newest("plan", Plan, lambda p, c=case: p.product == c.product and p.mode == "slice")
        if blocks is None:
            print(f"{case.product}: no stored run for {case.asset_id}, skipped")
            continue
        row = {"product": case.product, **locate_scores(blocks, case.human),
               "overlap": overlap_score(plan, case.human) if plan else None}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    print(json.dumps(summary(rows), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
