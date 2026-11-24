"""Model access. Nodes and agents never hold a model client; they ask for a tier.

One OpenAI-compatible endpoint (the company relay) serves every tier, so changing provider means
changing .env, not code. SDK retries are off (`max_retries=0`): retries happen in the agent loop,
where every attempt and its problems are recorded.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from ..settings import Settings

Tier = Literal["reasoning", "creative", "fast", "vision"]

# From the V2.0 tech plan, table 7. `vision` uses LLM_VISION_MODEL (a model that accepts images).
TIERS: dict[Tier, dict] = {
    "reasoning": {"temperature": 0.2, "timeout": 120},
    "creative": {"temperature": 0.6, "timeout": 120},
    "fast": {"temperature": 0.1, "timeout": 60},
    "vision": {"temperature": 0.1, "timeout": 120},
}


@dataclass
class Reply:
    """What a model returned for one structured call: raw arguments (not yet validated) + usage."""

    args: dict
    model: str
    seconds: float
    tokens_in: int = 0
    tokens_out: int = 0


class StructuredModel(Protocol):
    name: str

    def call(self, messages: list[BaseMessage], schema: type[BaseModel]) -> Reply:
        """Ask the model to answer by filling `schema`. Returns the raw arguments; the caller validates."""
        ...


class ModelProvider(Protocol):
    def get(self, tier: Tier) -> StructuredModel: ...


@dataclass
class ChatModel:
    """A LangChain ChatOpenAI pointed at the relay, forced to answer through one tool (= the schema)."""

    name: str
    tier: Tier
    settings: Settings = field(repr=False)

    def call(self, messages: list[BaseMessage], schema: type[BaseModel]) -> Reply:
        from langchain_openai import ChatOpenAI  # imported here so tests and graph drawing need no endpoint

        cfg = TIERS[self.tier]
        llm = ChatOpenAI(
            model=self.name, base_url=f"{self.settings.llm_base_url}/v1", api_key=self.settings.llm_api_key,
            temperature=cfg["temperature"], timeout=cfg["timeout"], max_retries=0,
        )
        # Function calling works on every model the relay offers (probe_models.py); json_schema mode does not.
        bound = llm.bind_tools([schema], tool_choice=schema.__name__)
        t0 = time.monotonic()
        msg = bound.invoke(messages)
        calls = getattr(msg, "tool_calls", None) or []
        args = calls[0]["args"] if calls else _json_or_empty(msg.content)
        usage = getattr(msg, "usage_metadata", None) or {}
        return Reply(args=args, model=self.name, seconds=time.monotonic() - t0,
                     tokens_in=usage.get("input_tokens", 0), tokens_out=usage.get("output_tokens", 0))


def _json_or_empty(content) -> dict:
    try:
        value = json.loads(content) if isinstance(content, str) else {}
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


@dataclass
class RelayModels:
    """The production provider: every tier on the relay from .env."""

    settings: Settings

    def get(self, tier: Tier) -> StructuredModel:
        if not self.settings.llm_base_url or not self.settings.llm_api_key:
            raise RuntimeError("LLM_BASE_URL / LLM_API_KEY missing from .env")
        name = self.settings.llm_vision_model if tier == "vision" else self.settings.llm_model
        return ChatModel(name=name, tier=tier, settings=self.settings)
