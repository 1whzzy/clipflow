"""What every node can use besides the state: storage, models, workers, rules, limits.

Passed per run as LangGraph runtime context (`app.invoke(..., context=RunContext(...))`), so
tests swap in fakes and production swaps in real workers without touching the graph.
"""
from dataclasses import dataclass, field

from ..llm import ModelProvider
from ..rules import Rules
from ..storage import ArtifactStore
from ..workers import ASR, TTS, AssetLibrary, CaseLibrary, Catalog, FrameSampler, Renderer


@dataclass(frozen=True)
class Limits:
    review_rounds: int = 1  # automatic reworks before handing the best version to a human. On the real
                            # runs the 1st rework helped and the 2nd never did (once it made the plan worse).
    qc_rounds: int = 2  # qc → rebuild timeline for fixable problems
    video_rounds: int = 5  # human edit rounds on one video (spec 3.3)
    frame_every_ms: int = 2000  # look: one frame every 2 s inside product blocks
    frames_per_call: int = 12
    model_concurrency: int = 6  # parallel model calls per step (relay rate limit; V2.0 §3.4)
    locate_lines_per_call: int = 600  # same chunk size as the prototype (run_job.CHUNK_LINES)


@dataclass
class RunContext:
    store: ArtifactStore
    models: ModelProvider
    asr: ASR
    frames: FrameSampler
    tts: TTS
    renderer: Renderer
    library: AssetLibrary
    cases: CaseLibrary
    catalog: Catalog
    rules: Rules = field(default_factory=Rules.load)
    limits: Limits = field(default_factory=Limits)
