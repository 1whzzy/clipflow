"""Graph state: what the checkpointer saves after every step.

It holds ids and small control fields only. Transcripts, plans and timelines live in the artifact
store (a 4-hour transcript is 7,468 lines; it must not be copied into every checkpoint).

Each field has exactly one writer node, so the order of execution never matters. The two list
fields are append-only, with an explicit reducer.
"""
from typing import Annotated, Literal, TypedDict


def append_new(left: list | None, right: list | None) -> list:
    """Append-only, ignoring items already present. Needed because a subgraph hands its whole state
    back to the parent graph: a plain `add` would append the subgraph's copy of the list again."""
    out = list(left or [])
    seen = {_key(x) for x in out}
    for x in right or []:
        if _key(x) not in seen:
            out.append(x)
            seen.add(_key(x))
    return out


def _key(x):
    return x["id"] if isinstance(x, dict) and "id" in x else repr(x)

# stopped = ended by a rule (banned word) or a limit; failed = a technical step kept failing
# (an admin can fix the cause and resume from the last checkpoint)
Status = Literal["running", "needs_input", "done", "rejected", "stopped", "cancelled", "failed"]


class RunState(TypedDict, total=False):
    # input: the RunRequest as JSON (validated by intake)
    input: dict

    # identity ----------------------------------------------------- writer
    run_id: str                       # intake
    mode: Literal["slice", "material"]  # intake
    request: str                      # intake          artifact id of RunRequest

    # slice front --------------------------------------------------
    transcript: str                   # transcribe
    time_hint: dict | None            # locate_product  user's rough range after "not found"
    blocks: str | None                # locate_product (look drops blocks of another item)
    clauses: str                      # refine_words
    frame_tags: str                   # look
    quotes: str                       # select_quotes

    # material front -----------------------------------------------
    script_lines: str                 # prepare_script

    # plan stage ---------------------------------------------------
    selling: str                      # storyboard
    plan: str                         # storyboard, match_assets, confirm_plan (edit), review_video (edit), tts
    review: str                       # auto_review
    review_round: int                 # auto_review (reset by review_video)
    best_plan: dict | None            # auto_review: {plan, review, score} of the best agent-made version so far
    plan_decision: str | None         # confirm_plan
    phase: Literal["plan", "video"]   # intake / review_video: which pause the latest Plan change came from
    plan_by: Literal["agent", "human"]  # storyboard / the two pauses: who made the latest Plan change.
                                      # After a human edit, findings go back to the human, never to an agent
                                      # (an automatic rework would throw the reviewer's edits away).

    # video stage --------------------------------------------------
    voice: str                        # tts
    timeline: str                     # build_timeline
    render: str                       # render
    qc: str                           # qc
    qc_round: int                     # qc
    video_decision: str               # review_video
    video_round: int                  # review_video
    delivery: str                     # deliver

    # control ------------------------------------------------------
    status: Status                    # intake, and the node that ends the run
    flags: Annotated[list[str], append_new]  # any node: risks and degraded steps, shown to the reviewer
    trace: Annotated[list[dict], append_new]  # any node: one entry per agent call (id, prompt, attempts, tokens, cache)
