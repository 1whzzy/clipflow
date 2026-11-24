"""Check whether the company API gateway can run the agent.

Reads LLM_BASE_URL, LLM_API_KEY and LLM_MODEL from .env in the repo root (see .env.example).
Never prints the key.

  python pilot/scripts/check_llm.py

Tests, in order:
  1. reachability: GET <base>/v1/models (lists the models the gateway offers, if it allows that)
  2. a plain Messages API call (Chinese prompt)
  3. a real agent step: tool use on a slice of the Nov 10 replay transcript
"""
import json
import sys
import time
import urllib.error
import urllib.request

import anthropic

import common

DEFAULT_MODEL = "claude-opus-5-5"


def load_env():
    path = common.ROOT / ".env"
    if not path.exists():
        sys.exit("No .env found. Copy .env.example to .env and fill in LLM_BASE_URL and LLM_API_KEY.")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    base, key = env.get("LLM_BASE_URL", "").rstrip("/"), env.get("LLM_API_KEY", "")
    if not base or not key:
        sys.exit(".env is missing LLM_BASE_URL or LLM_API_KEY.")
    if base.endswith("/v1"):
        base = base[:-3]
    return base, key, env.get("LLM_MODEL") or DEFAULT_MODEL


def list_models(base, key):
    """Diagnostic only: which models does the gateway expose? Tries both common auth headers."""
    for label, headers in [("x-api-key", {"x-api-key": key, "anthropic-version": "2023-06-01"}),
                           ("Bearer", {"Authorization": f"Bearer {key}"})]:
        req = urllib.request.Request(f"{base}/v1/models", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            ids = [m.get("id") for m in data.get("data", [])]
            print(f"  OK with {label} auth: {len(ids)} models")
            for i in ids[:40]:
                print(f"    - {i}")
            return label
        except urllib.error.HTTPError as e:
            print(f"  {label} auth: HTTP {e.code}")
        except Exception as e:
            print(f"  {label} auth: {type(e).__name__}: {e}")
    return None


def make_client(base, key, auth):
    # Gateways differ: some want the Anthropic x-api-key header, some a Bearer token.
    if auth == "Bearer":
        return anthropic.Anthropic(base_url=base, auth_token=key, max_retries=1, timeout=120)
    return anthropic.Anthropic(base_url=base, api_key=key, max_retries=1, timeout=120)


def call(client, **kwargs):
    t = time.time()
    try:
        r = client.messages.create(**kwargs)
        return r, time.time() - t, None
    except anthropic.AuthenticationError as e:
        return None, 0, f"authentication failed (401): {e.message}"
    except anthropic.PermissionDeniedError as e:
        return None, 0, f"permission denied (403): {e.message}"
    except anthropic.NotFoundError as e:
        return None, 0, f"not found (404), wrong URL or model name: {e.message}"
    except anthropic.BadRequestError as e:
        return None, 0, f"bad request (400): {e.message}"
    except anthropic.RateLimitError as e:
        return None, 0, f"rate limited (429): {e.message}"
    except anthropic.APIStatusError as e:
        return None, 0, f"HTTP {e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        return None, 0, f"connection error: {e}"


def transcript_slice():
    path = common.ROOT / "pilot" / "work" / "26_11_10 11_27 (2)" / "transcript.json"
    rows = common.load_json(path)["segments"]
    lines = [f"[{r['start']:.1f}-{r['end']:.1f}] {r['text']}" for r in rows if 200 <= r["start"] <= 420]
    return "\n".join(lines)


SEGMENT_TOOL = {
    "name": "submit_segments",
    "description": "Submit the chosen clip segments for the short selling video, in play order.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["segments"],
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["start", "end", "role", "hook", "reason"],
                    "properties": {
                        "start": {"type": "number", "description": "seconds in the source"},
                        "end": {"type": "number"},
                        "role": {"type": "string", "enum": ["opening", "middle", "ending"]},
                        "hook": {"type": "string", "description": "hook type for the opening, else empty"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
}


def main():
    base, key, model = load_env()
    print(f"Gateway: {base}\nModel:   {model}\nKey:     set ({len(key)} chars)\n")

    print("1. Reachability (GET /v1/models)")
    auth = list_models(base, key) or "x-api-key"

    print("\n2. Plain Messages API call")
    client = make_client(base, key, auth)
    r, secs, err = call(client, model=model, max_tokens=2000,
                        messages=[{"role": "user", "content": "用一句话介绍你自己。"}])
    if err and auth == "x-api-key" and "401" in err:
        print(f"  x-api-key failed ({err}); trying Bearer")
        client = make_client(base, key, "Bearer")
        r, secs, err = call(client, model=model, max_tokens=2000,
                            messages=[{"role": "user", "content": "用一句话介绍你自己。"}])
    if err:
        print(f"  FAILED: {err}")
        print("\nThe gateway can't be used through the Anthropic Messages API as configured.")
        print("Check the URL and model name with whoever runs the gateway (is it Anthropic-compatible?).")
        sys.exit(1)
    text = next((b.text for b in r.content if b.type == "text"), "")
    print(f"  OK in {secs:.1f}s, served by {r.model}: {text.strip()[:120]}")
    print(f"  tokens in/out: {r.usage.input_tokens}/{r.usage.output_tokens}")

    print("\n3. Agent step: pick segments from a real transcript slice with tool use")
    prompt = (
        "下面是一场女装直播的转写片段（时间单位：秒）。主播在卖一条显瘦的裤子。"
        "请挑出 2–4 段做成 30–45 秒的带货短视频：第一段是开头钩子，最后一段是价格或下单号召。"
        "只用和这条裤子有关的句子，每段起止落在整句上。完成后调用 submit_segments 工具提交。\n\n"
        + transcript_slice()
    )
    r, secs, err = call(client, model=model, max_tokens=16000, tools=[SEGMENT_TOOL],
                        tool_choice={"type": "auto"},
                        messages=[{"role": "user", "content": prompt}])
    if err:
        print(f"  FAILED: {err}")
        sys.exit(1)
    if r.stop_reason == "refusal":
        print("  model refused the request")
        sys.exit(1)
    calls = [b for b in r.content if b.type == "tool_use" and b.name == "submit_segments"]
    if not calls:
        print(f"  no tool call (stop_reason={r.stop_reason}); the gateway or model may not support tool use")
        sys.exit(1)
    segs = calls[0].input["segments"]
    total = sum(s["end"] - s["start"] for s in segs)
    print(f"  OK in {secs:.1f}s: {len(segs)} segments, {total:.0f}s total")
    for s in segs:
        print(f"    {s['role']:8} {s['start']:7.1f}-{s['end']:7.1f}  {s['hook']:6}  {s['reason'][:60]}")
    print(f"  tokens in/out: {r.usage.input_tokens}/{r.usage.output_tokens}")
    print("\nGateway works for the agent role.")


if __name__ == "__main__":
    main()
