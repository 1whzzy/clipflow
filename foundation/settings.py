"""Platform settings from the environment and .env. Separate from clipflow's on purpose: the platform
must not import a workflow's code."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path = ROOT / ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: v for k, v in os.environ.items() if k.startswith(("DATABASE_", "FOUNDATION_"))})
    return values


def valid_schema(name: str) -> str:
    """A Postgres schema name we create ourselves: lower-case letters, digits and _, max 40 chars."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
        raise ValueError(f"invalid schema name {name!r}")
    return name


@dataclass(frozen=True)
class PlatformSettings:
    database_url: str = field(default="", repr=False)
    schema: str = "foundation"  # all platform tables live in this Postgres schema
    files_dir: Path = ROOT / "pilot" / "work" / "platform"  # delivered files (git-ignored)
    api_port: int = 8100
    brand: str = "智能工作平台"  # the platform's name on its pages (FOUNDATION_BRAND)
    tagline: str = "把重复的内容生产交给 AI，关键的地方由人把关"  # FOUNDATION_TAGLINE

    @classmethod
    def load(cls) -> PlatformSettings:
        env = read_env()
        return cls(
            database_url=env.get("DATABASE_URL", ""),
            schema=valid_schema(env.get("FOUNDATION_SCHEMA", "foundation")),
            files_dir=Path(env["FOUNDATION_FILES"]) if env.get("FOUNDATION_FILES") else cls.files_dir,
            api_port=int(env.get("FOUNDATION_API_PORT", "8100")),
            brand=env.get("FOUNDATION_BRAND") or cls.brand,
            tagline=env.get("FOUNDATION_TAGLINE") or cls.tagline,
        )
