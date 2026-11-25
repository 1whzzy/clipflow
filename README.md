# 短视频带货剪辑助手 AI 平台 V2.0

Turns a product and its footage into a finished vertical selling video for Douyin. People confirm the plan (optional) and review the video; the system does the rest.

| Folder | What |
|---|---|
| `clipflow/` | V2.0 workflow on LangGraph + LangChain: 18 nodes, 8 agents. Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). |
| `pilot/` | The working slice-mode prototype (`run_job.py`), to be wrapped by `clipflow/workers/pilot.py` |
| `config.yaml` | Paths, output spec and the rule library |
| `examples/` | 替换了真实商品和素材信息的商品、任务和评测示例 |

## 评审材料

- [项目业务说明](短视频带货剪辑项目业务说明.docx)
- [V2.0 技术方案流程图](技术方案V2.0流程图续图.png)
- [架构说明](docs/ARCHITECTURE.md)与[当前代码生成的流程图](docs/workflow.mmd)

## Setup

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the gateway and database values. Never commit `.env`.

## Verification

本地完整工作区通过 80 项测试和 Ruff 检查。架构说明记录了出片和数据库恢复的验收范围。

## Run a real job

Copy the example job and product YAML files to local `jobs/` and `products/`, then set their paths to available media.

```powershell
.\.venv\Scripts\python.exe -m clipflow.run jobs\示例裤A-001.yaml
```

Answers each pause in the terminal (`--auto` for a scripted smoke test). With Postgres running, `--stop-at-pause` exits at the first pause and `--thread <id>` picks the run up later, from any process. A first run on a transcribed replay takes about 6 minutes; a re-run with caches under a minute. It prints the time of each step.

## HTTP API

```powershell
.\.venv\Scripts\python.exe -m clipflow.api
```

Serves `/api/v1/runs` on http://127.0.0.1:8000; interactive docs at http://127.0.0.1:8000/docs.

## Database (for saved runs)

```powershell
docker compose up -d
```
```powershell
docker compose ps postgres
```
