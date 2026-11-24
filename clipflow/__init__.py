"""clipflow: the V2.0 workflow (product + footage → finished selling video) on LangGraph + LangChain.

Layers, top to bottom:
    graph/      the 18 workflow nodes wired as a LangGraph StateGraph (order, branches, loops, pauses)
    nodes/      thin step functions: read refs from state, call an agent / worker / plain code, store results
    agents/     the 8 LLM agents (LangChain): input contract → output contract, checked by code
    contracts/  Pydantic models for everything passed between steps
    workers/    long-running media work (ASR, frames, TTS, ffmpeg) behind interfaces
    storage/    artifact store (big objects live here, state holds their ids) and the checkpointer

See docs/ARCHITECTURE.md.
"""
