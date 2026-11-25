r"""Run one job through the workflow with the real workers and the LLM relay.

    .\.venv\Scripts\python.exe -m clipflow.run jobs\示例裤A-001.yaml            # answer pauses in the terminal
    .\.venv\Scripts\python.exe -m clipflow.run jobs\示例裤A-001.yaml --auto     # smoke test, see below

At each pause the payload is printed and a JSON answer is read from the terminal, e.g.
    {"verdict": "approve", "version": 1}
    {"verdict": "edit", "version": 1, "edits": [{"kind": "delete", "seq": 9}]}
    {"start": "4:00", "end": "9:30"}            (product not found: a rough time range)

--auto is a scripted reviewer for smoke tests, not a policy: it deletes shots with must-fix
findings, approves with the reason "smoke test" otherwise, and cancels when the product isn't found.
State is saved in Postgres when DATABASE_URL is set and reachable; otherwise in memory.
"""
import argparse
import json
import sys
import time
import uuid

import yaml
from langgraph.types import Command

from .contracts import Delivery, Plan, RunRequest
from .graph.build import compile_app
from .graph.context import RunContext
from .llm import RelayModels
from .settings import ROOT, Settings
from .storage import FileStore, open_checkpointer
from .workers.pilot import NoCases, PilotASR, PilotFrames, PilotLibrary, PilotRenderer, WindowsTTS, YamlCatalog


def real_context(settings: Settings, store: str | None = None) -> RunContext:
    """The workers for this workstation: the LLM relay, the prototype's ASR/frames/render, Windows TTS."""
    return RunContext(store=FileStore(store or settings.artifacts_dir), models=RelayModels(settings), asr=PilotASR(),
                      frames=PilotFrames(), tts=WindowsTTS(), renderer=PilotRenderer(), library=PilotLibrary(),
                      cases=NoCases(), catalog=YamlCatalog())


def reachable_db(settings: Settings) -> str | None:
    """DATABASE_URL if Postgres answers within 3 s, else None (run state stays in memory)."""
    if not settings.database_url:
        return None
    try:
        import psycopg
        psycopg.connect(settings.database_url, connect_timeout=3).close()
        return settings.database_url
    except Exception as e:  # noqa: BLE001
        print(f"Postgres not reachable ({type(e).__name__}); keeping run state in memory.")
        return None


def request_from_job(path: str) -> dict:
    """The prototype's job files (jobs/*.yaml) → a RunRequest. Only the product id is passed: intake
    loads the sheet from the catalog."""
    job = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    req = job.get("requirements", {})
    lo, hi = req.get("length", [30, 45])
    replays = job.get("sources", {}).get("replays", [])
    return RunRequest(
        run_id=f"{job['product']}-{time.strftime('%m%d-%H%M%S')}", mode="slice" if replays else "material",
        product_id=job["product"],
        source_videos=[{"asset_id": r["file"], "start_ms": r.get("start"), "end_ms": r.get("end")} for r in replays],
        script=job.get("script"),  # material mode: {text, status: final|draft}
        options={"target_ms": (lo * 1000, hi * 1000), "hook_keywords": req.get("hook_keywords", []),
                 "must_mention": req.get("must_mention", []), "hook_style": req.get("hook_style", "any"),
                 "focus": req.get("focus", []), "cta": req.get("cta", ""),
                 "confirm_plan": req.get("confirm_plan", True)},
    ).model_dump(mode="json")


def auto_answer(pause: dict) -> dict:
    if pause["kind"] == "needs_input":
        return {"action": "cancel"}
    if pause.get("error"):
        print(f"  ! {pause['error']}")
    fix = sorted({int(m.split("：")[0].replace("镜头", "").strip()) for m in pause.get("must_fix", [])
                  if m.startswith("镜头")})
    if fix:
        return {"verdict": "edit", "version": pause["version"], "edits": [{"kind": "delete", "seq": s} for s in fix]}
    if "approve" in pause["actions"]:
        return {"verdict": "approve", "version": pause["version"], "reason": "smoke test"}
    return {"verdict": "reject", "version": pause["version"], "reason": "smoke test: cannot be fixed automatically"}


def drive(app, inp, cfg, ctx, timings: dict) -> list:
    """Run until the next pause or the end; add each step's wall time to `timings`.
    Returns the pending interrupts (empty when the run ended)."""
    last, pauses = time.monotonic(), []
    for _ns, update in app.stream(inp, cfg, context=ctx, stream_mode="updates", subgraphs=True):
        now = time.monotonic()
        for node in update:
            if node == "__interrupt__":
                pauses = list(update[node])
            elif node != "slice_front":  # the subgraph's own steps are timed individually
                timings[node] = timings.get(node, 0.0) + (now - last)
        last = now
    return pauses


def continue_thread(app, cfg, ctx, timings: dict) -> list:
    """Pick up an existing thread from its last checkpoint (e.g. in a new process, after a crash).

    - waiting at a pause: return that pause, so it can be answered;
    - stopped by an error: run on from the step that failed (input None = "continue");
    - finished: nothing to do.
    Note: `Command(resume=None)` is not the same as None. It is an answer to a pause, and LangGraph
    fails on it when there is no pause to answer."""
    snap = app.get_state(cfg)
    if not snap.next:
        return []
    waiting = [i for task in snap.tasks for i in task.interrupts]
    if waiting:
        return waiting
    return drive(app, None, cfg, ctx, timings)


def show(pause: dict) -> None:
    view = {k: v for k, v in pause.items() if k not in ("plan",)}
    if "plan" in pause:
        view["shots"] = [f"{s['seq']}. [{s['purpose']}/{s['shot_type']}] {s['line']}" for s in pause["plan"]["shots"]]
    print(json.dumps(view, ensure_ascii=False, indent=1))


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("job", help="a job file, e.g. jobs/示例裤A-001.yaml")
    p.add_argument("--auto", action="store_true", help="scripted reviewer for smoke tests")
    p.add_argument("--thread", help="resume this thread (needs Postgres)")
    p.add_argument("--store", help="artifact/cache folder (default: pilot/work/artifacts)")
    p.add_argument("--stop-at-pause", action="store_true",
                   help="exit at the first pause, leaving the run in Postgres to answer later with --thread")
    args = p.parse_args(argv)

    settings = Settings.load()
    ctx = real_context(settings, args.store)
    with open_checkpointer(reachable_db(settings)) as saver:
        app = compile_app(saver)
        thread = args.thread or f"run-{uuid.uuid4().hex[:8]}"
        cfg = {"configurable": {"thread_id": thread}, "recursion_limit": 80}
        t0 = time.time()
        timings: dict[str, float] = {}
        try:
            if args.thread:
                pauses = continue_thread(app, cfg, ctx, timings)
            else:
                pauses = drive(app, {"input": request_from_job(args.job)}, cfg, ctx, timings)
            while pauses:
                pause = pauses[0].value
                print(f"\n== paused: {pause['kind']} (thread {thread}) ==")
                show(pause)
                if args.stop_at_pause:
                    print(f"stopped at the pause; answer it later with --thread {thread}")
                    return 3
                answer = auto_answer(pause) if args.auto else json.loads(input("answer (JSON)> "))
                print(f"-> {json.dumps(answer, ensure_ascii=False)}")
                pauses = drive(app, Command(resume=answer), cfg, ctx, timings)
        except Exception as e:  # noqa: BLE001  a step kept failing: the last checkpoint is kept
            snap = app.get_state(cfg)
            print(f"\n== failed at {list(snap.next) or '?'}: {type(e).__name__}: {e}")
            print(f"Fix the cause, then resume from the last checkpoint with --thread {thread} (needs Postgres).")
            return 2
        state = app.get_state(cfg).values
        print(f"\n== {state.get('status')} in {time.time() - t0:.0f}s ==")
        print("time per step (s):", ", ".join(f"{k} {v:.1f}" for k, v in sorted(timings.items(), key=lambda x: -x[1])))
        print("flags:", *state.get("flags", []), sep="\n  ")
        calls = [t for t in state.get("trace", []) if not t["cached"]]
        by_node: dict[str, float] = {}
        for t in calls:
            by_node[t["node"]] = by_node.get(t["node"], 0.0) + t.get("seconds", 0.0)
        slowest = sorted(by_node.items(), key=lambda x: -x[1])
        print("model seconds per node:", ", ".join(f"{k} {v:.0f}" for k, v in slowest))
        print(f"model calls: {sum(t['attempts'] for t in calls)} "
              f"({sum(t['tokens_in'] for t in calls)} in / {sum(t['tokens_out'] for t in calls)} out tokens), "
              f"cached steps: {sum(t['cached'] for t in state.get('trace', []))}")
        if state.get("plan"):
            plan = ctx.store.get(state["plan"], Plan)
            print(f"plan v{plan.version}: {len(plan.shots)} shots, {plan.total_ms / 1000:.1f} s")
        if state.get("delivery"):
            print("video:", ctx.store.get(state["delivery"], Delivery).video_path)
        return 0 if state.get("status") == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
