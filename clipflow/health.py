"""Clipflow's own health, shown on the workbench's 运行状态 page (foundation/registry.py CHECKS).

- model gateway: a one-token request, timed. The gateway timed out for an afternoon on 2026-11-26
  and every clip and intake task waited on it; this makes that visible before people wonder.
- GPU: is it there, how much memory is in use (transcription runs on it).

Each check returns {name, ok, message, ms}. A check never raises: a failure is a result.
"""
import subprocess
import time

SLOW_MS = 20_000  # an answer this slow makes 60-120 s agent calls likely to time out


def gateway() -> dict:
    import httpx

    from .settings import Settings

    s = Settings.load()
    if not s.llm_base_url or not s.llm_api_key:
        return {"name": "模型网关", "ok": False, "message": "没有配置（.env 里的 LLM_BASE_URL / LLM_API_KEY）"}
    t0 = time.monotonic()
    try:
        r = httpx.post(f"{s.llm_base_url}/v1/chat/completions", timeout=60,
                       headers={"Authorization": f"Bearer {s.llm_api_key}"},
                       json={"model": s.llm_model, "max_tokens": 1,
                             "messages": [{"role": "user", "content": "ping"}]})
        ms = round((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return {"name": "模型网关", "ok": False, "ms": ms,
                    "message": f"模型网关返回 {r.status_code}：出片和入库会失败"}
        if ms > SLOW_MS:
            return {"name": "模型网关", "ok": False, "ms": ms,
                    "message": f"模型网关很慢（{ms / 1000:.0f} 秒才回一个字）：出片和入库可能超时"}
        return {"name": "模型网关", "ok": True, "ms": ms, "message": f"正常，{ms / 1000:.1f} 秒响应（{s.llm_model}）"}
    except httpx.HTTPError as e:
        ms = round((time.monotonic() - t0) * 1000)
        return {"name": "模型网关", "ok": False, "ms": ms,
                "message": f"模型网关没有响应（{type(e).__name__}）：出片和入库会失败"}


def gpu() -> dict:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10, check=True)
        name, used, total, util = [x.strip() for x in out.stdout.splitlines()[0].split(",")]
        memory = f"{int(used) / 1024:.1f}/{int(total) / 1024:.0f} GB"
        return {"name": "GPU", "ok": True, "message": f"{name}，显存 {memory}，使用率 {util}%"}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        return {"name": "GPU", "ok": False, "message": f"找不到 GPU（{type(e).__name__}）：语音转写会非常慢"}


def checks() -> list[dict]:
    return [gateway(), gpu()]
