"""Thin wrapper around the company LLM gateway (OpenAI-compatible Chat Completions).

The agent uses one pattern everywhere: ask the model to do a task and submit the result by
calling a named tool; validate the arguments in code; if they fail, send the problems back
and let the model try again.
"""
import base64
import json
import time
from pathlib import Path

from openai import OpenAI, APIConnectionError, APIStatusError, RateLimitError

import common
from check_llm import load_env


def env_value(name):
    """Read one optional value from .env (load_env only returns the required ones)."""
    path = common.ROOT / ".env"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def image_part(path):
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}", "detail": "low"}}


class LLM:
    def __init__(self, model=None):
        base, key, env_model = load_env()
        self.model = model or env_model
        self.client = OpenAI(base_url=f"{base}/v1", api_key=key, timeout=300, max_retries=2)
        self.calls = []  # one record per API call, for the run log

    def submit(self, system, user, tool_name, tool_description, schema, validate=None, max_attempts=3, label="",
               images=None):
        """Run until the model calls `tool_name` with arguments that pass `validate`.

        validate(args) returns a list of problems (empty = OK). Returns the parsed arguments.
        images: optional list of JPEG paths sent with the user message (needs a vision model).
        """
        tool = {"type": "function", "function": {
            "name": tool_name, "description": tool_description, "parameters": schema}}
        content = user
        if images:
            content = [{"type": "text", "text": user}] + [image_part(p) for p in images]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        for attempt in range(1, max_attempts + 1):
            t = time.time()
            try:
                r = self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=[tool], tool_choice="required")
            except RateLimitError:
                time.sleep(20)
                continue
            except (APIStatusError, APIConnectionError) as e:
                raise RuntimeError(f"LLM call failed ({label}): {e}") from e
            msg = r.choices[0].message
            self.calls.append({"label": label, "attempt": attempt, "model": r.model,
                               "seconds": round(time.time() - t, 1),
                               "prompt_tokens": r.usage.prompt_tokens if r.usage else None,
                               "completion_tokens": r.usage.completion_tokens if r.usage else None})
            calls = [c for c in (msg.tool_calls or []) if c.function.name == tool_name]
            if not calls:
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": f"请调用 {tool_name} 工具提交结果。"})
                continue
            call = calls[0]
            try:
                args = json.loads(call.function.arguments)
                problems = validate(args) if validate else []
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                problems = [f"参数无法解析：{e}"]
            if not problems:
                return args
            print(f"  attempt {attempt}: {len(problems)} problem(s): {problems[0]}")
            messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                {"id": call.id, "type": "function",
                 "function": {"name": tool_name, "arguments": call.function.arguments}}]})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": "未通过检查，请修正后重新调用工具：\n- " + "\n- ".join(problems)})
        raise RuntimeError(f"{label}: no valid result after {max_attempts} attempts")

    def usage(self):
        return {"calls": len(self.calls),
                "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in self.calls),
                "completion_tokens": sum(c["completion_tokens"] or 0 for c in self.calls),
                "seconds": round(sum(c["seconds"] for c in self.calls), 1)}
