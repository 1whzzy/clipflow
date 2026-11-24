"""Nodes 8 and 13: the material-mode steps (script → lines; lines → voice)."""
from langgraph.runtime import Runtime

from ..agents import PrepareScriptAgent
from ..agents.script import ScriptTaskInput, to_script_lines
from ..contracts import Plan, ScriptInput, ScriptLines, Speech, VoiceTrack
from ..graph.context import RunContext
from ..graph.state import RunState
from ._util import load, request, rework_notes, trace


def prepare_script(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """8. 脚本生成 Agent (material task). A final script is only split into lines, never reworded."""
    ctx = runtime.context
    req = request(ctx, state)
    script = req.script or ScriptInput(text="", status="draft")  # no script: write a draft from the product sheet
    inp = ScriptTaskInput(product=req.product, script=script, focus=req.options.focus,
                          rework_notes=rework_notes(ctx, state, "content"))
    result = PrepareScriptAgent().run_cached(inp, ctx.models, ctx.store)
    return {"script_lines": ctx.store.put("script_lines", to_script_lines(inp, result.output)),
            "trace": [trace("prepare_script", result)]}


def tts(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """13. Voice-over, one clip per line. The real durations replace the estimates in the Plan."""
    ctx = runtime.context
    req = request(ctx, state)
    lines = load(ctx, state, "script_lines", ScriptLines)
    clips = ctx.tts.speak(req.run_id, lines.lines, req.options.voice)
    plan = load(ctx, state, "plan", Plan)
    if len(clips) != len(lines.lines):
        raise RuntimeError(f"TTS returned {len(clips)} clips for {len(lines.lines)} lines")
    # match by line seq, not by position: the plan may have been edited (shots deleted or reordered)
    by_seq = {line.seq: clip for line, clip in zip(lines.lines, clips)}
    shots = []
    for s in plan.shots:
        clip = by_seq.get(s.seq)
        if clip is None:
            shots.append(s)
            continue
        speech = Speech(kind="voice", asset_id=clip.asset_id, in_ms=clip.in_ms, out_ms=clip.out_ms)
        shots.append(s.model_copy(update={"speech": speech, "duration_ms": clip.out_ms - clip.in_ms}))
    return {"voice": ctx.store.put("voice", VoiceTrack(run_id=req.run_id, clips=clips)),
            "plan": ctx.store.put("plan", plan.model_copy(update={"shots": shots}))}
