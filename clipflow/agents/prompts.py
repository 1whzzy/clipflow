"""Prompt files: clipflow/prompts/<prompt_id>.v<N>.md, a YAML header plus the prompt body.

Every prompt uses the same six-part outline (V2.0 tech plan §5.7), in this order, so reviews can
compare prompts section by section and A/B tests can change one section at a time:
    角色与边界 / 输入数据说明 / 输出契约 / 判断规则 / 禁止事项 / 示例
"""
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
SECTIONS = ["角色与边界", "输入数据说明", "输出契约", "判断规则", "禁止事项", "示例"]


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    version: int
    tier: str
    owner: str
    eval_set: str
    body: str

    @property
    def ref(self) -> str:
        return f"{self.prompt_id}.v{self.version}"


@cache
def load_prompt(prompt_id: str, version: int | None = None) -> Prompt:
    """Load one version, or the highest one when `version` is None."""
    files = sorted(PROMPTS.glob(f"{prompt_id}.v*.md"), key=lambda p: int(p.stem.rsplit(".v", 1)[1]))
    if version is not None:
        files = [p for p in files if p.stem == f"{prompt_id}.v{version}"]
    if not files:
        raise FileNotFoundError(f"no prompt file for {prompt_id} (v{version or '*'}) in {PROMPTS}")
    text = files[-1].read_text(encoding="utf-8")
    _, header, body = text.split("---", 2)
    meta = yaml.safe_load(header)
    missing = [s for s in SECTIONS if f"## {s}" not in body]
    if missing:
        raise ValueError(f"{files[-1].name}: missing sections {missing}")
    return Prompt(prompt_id=meta["prompt_id"], version=int(meta["version"]), tier=meta["tier"],
                  owner=meta.get("owner", ""), eval_set=meta.get("eval_set", ""), body=body.strip())
