"""The workflow graph: 18 nodes, one subgraph, every branch, loop and human pause.

    intake → route_mode ─┬─ slice_front (subgraph: transcribe → locate_product → refine_words → look → select_quotes)
                         └─ prepare_script
    → storyboard → match_assets → auto_review ─(rework ≤2)─┐
                                       │ ◄─────────────────┘
                                       ▼
                       confirm_plan (pause) → [tts] → build_timeline → render → qc ─(fixable ≤2)─► build_timeline
                                                                                 ▼
                                               review_video (pause) ─(edits ≤5)─► auto_review / build_timeline
                                                                                 ▼
                                                                              deliver

Only the slice front is a subgraph: it is mode-specific, linear, and owns its own loop ("not
found → ask for a time range"). The shared stages stay in the main graph so the review, QC and
video-edit loops are plain edges that can be read in one picture.
"""
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from ..nodes.intake import intake, route_mode
from ..nodes.material import prepare_script, tts
from ..nodes.plan import auto_review, confirm_plan, match_assets, storyboard
from ..nodes.slice import locate_product, look, refine_words, select_quotes, transcribe
from ..nodes.video import build_timeline, deliver, qc, render, review_video
from . import routing
from .context import RunContext
from .state import RunState

def transient(exc: Exception) -> bool:
    """Only failures that a second try can fix: timeouts, dropped connections, rate limits, a 5xx
    from the model relay. Everything else is not retried here:
    - AgentFailed: the agent already asked 3 times with its problems listed; asking 3 more times
      would only repeat that (LangGraph's default would retry it, up to 9 calls in total);
    - bad input, a missing file, an ffmpeg error: the same input fails the same way."""
    import openai

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # APIConnectionError includes APITimeoutError
    return isinstance(exc, (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError))


# Transient failures only (see `transient`); contract problems are handled inside each agent's loop.
MODEL = RetryPolicy(max_attempts=3, initial_interval=2.0, backoff_factor=2.0, retry_on=transient)
WORKER = RetryPolicy(max_attempts=2, initial_interval=5.0, retry_on=transient)

NODES = [  # (number, name, kind) — the 18 workflow nodes, as in the meeting brief and the spec
    (1, "intake", "code"), (2, "route_mode", "code + branch"),
    (3, "transcribe", "worker (GPU)"), (4, "locate_product", "agent 商品定位 + pause"),
    (5, "refine_words", "worker (GPU)"), (6, "look", "agent 画面理解"), (7, "select_quotes", "agent 脚本生成"),
    (8, "prepare_script", "agent 脚本生成"), (9, "storyboard", "agents 卖点匹配 + 分镜生成"),
    (10, "match_assets", "code + agent 素材召回"), (11, "auto_review", "rules + agent 合规审核"),
    (12, "confirm_plan", "human pause (optional)"), (13, "tts", "worker"), (14, "build_timeline", "code"),
    (15, "render", "worker (ffmpeg)"), (16, "qc", "code"), (17, "review_video", "human pause"),
    (18, "deliver", "code"),
]


def build_slice_front() -> StateGraph:
    g = StateGraph(RunState, context_schema=RunContext)
    g.add_node("transcribe", transcribe, retry_policy=WORKER)
    g.add_node("locate_product", locate_product, retry_policy=MODEL)
    g.add_node("refine_words", refine_words, retry_policy=WORKER)
    g.add_node("look", look, retry_policy=MODEL)
    g.add_node("select_quotes", select_quotes, retry_policy=MODEL)
    g.add_conditional_edges(START, routing.slice_entry, ["transcribe", "select_quotes"])
    g.add_edge("transcribe", "locate_product")
    g.add_conditional_edges("locate_product", routing.after_locate, ["refine_words", "locate_product", END])
    g.add_edge("refine_words", "look")
    g.add_edge("look", "select_quotes")
    g.add_edge("select_quotes", END)
    return g


def build_graph() -> StateGraph:
    g = StateGraph(RunState, context_schema=RunContext)
    g.add_node("intake", intake)
    g.add_node("route_mode", route_mode)
    g.add_node("slice_front", build_slice_front().compile())
    g.add_node("prepare_script", prepare_script, retry_policy=MODEL)
    g.add_node("storyboard", storyboard, retry_policy=MODEL)
    g.add_node("match_assets", match_assets, retry_policy=MODEL)
    g.add_node("auto_review", auto_review, retry_policy=MODEL)
    g.add_node("confirm_plan", confirm_plan)
    g.add_node("tts", tts, retry_policy=WORKER)
    g.add_node("build_timeline", build_timeline)
    g.add_node("render", render, retry_policy=WORKER)
    g.add_node("qc", qc, retry_policy=WORKER)
    g.add_node("review_video", review_video)
    g.add_node("deliver", deliver)

    g.add_edge(START, "intake")
    g.add_edge("intake", "route_mode")
    g.add_conditional_edges("route_mode", routing.after_route, ["slice_front", "prepare_script"])
    g.add_conditional_edges("slice_front", routing.after_slice_front, ["storyboard", END])
    g.add_edge("prepare_script", "storyboard")
    g.add_edge("storyboard", "match_assets")
    g.add_edge("match_assets", "auto_review")
    g.add_conditional_edges("auto_review", routing.after_review,
                            ["slice_front", "prepare_script", "storyboard", "match_assets", "confirm_plan",
                             "build_timeline", END])
    g.add_conditional_edges("confirm_plan", routing.after_confirm, ["tts", "build_timeline", "auto_review", END])
    g.add_edge("tts", "build_timeline")
    g.add_edge("build_timeline", "render")
    g.add_edge("render", "qc")
    g.add_conditional_edges("qc", routing.after_qc, ["build_timeline", "review_video", END])
    g.add_conditional_edges("review_video", routing.after_video_review,
                            ["deliver", "auto_review", "build_timeline", END])
    g.add_edge("deliver", END)
    return g


def compile_app(checkpointer):
    """The runnable workflow. A checkpointer is required: the two human pauses need saved state."""
    return build_graph().compile(checkpointer=checkpointer)


def mermaid() -> str:
    return build_graph().compile().get_graph(xray=True).draw_mermaid()


if __name__ == "__main__":  # python -m clipflow.graph.build → regenerate docs/workflow.mmd
    from pathlib import Path

    target = Path(__file__).resolve().parents[2] / "docs" / "workflow.mmd"
    target.write_text(mermaid(), encoding="utf-8", newline="\n")
    print(f"wrote {target}")
