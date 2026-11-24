"""Settings from .env and config.yaml. Nothing is read at import time."""
import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path = ROOT / ".env") -> dict[str, str]:
    """Minimal .env reader (KEY=value lines). Real environment variables win."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k in values or k.startswith(("LLM_", "DATABASE_"))})
    return values


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    llm_vision_model: str = ""
    database_url: str = field(default="", repr=False)
    artifacts_dir: Path = ROOT / "pilot" / "work" / "artifacts"

    @classmethod
    def load(cls) -> "Settings":
        env = read_env()
        return cls(
            llm_base_url=env.get("LLM_BASE_URL", "").rstrip("/"),
            llm_api_key=env.get("LLM_API_KEY", ""),
            llm_model=env.get("LLM_MODEL", ""),
            llm_vision_model=env.get("LLM_VISION_MODEL", "") or env.get("LLM_MODEL", ""),
            database_url=env.get("DATABASE_URL", ""),
        )


@cache
def config() -> dict:
    """config.yaml: paths, video spec and the rule library (`rules`, `selling_point_priority`)."""
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
