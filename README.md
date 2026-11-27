# 短视频带货剪辑助手 AI 平台 V2.0

Turns a product and its footage into a finished vertical selling video for Douyin. People confirm the plan (optional) and review the video; the system does the rest.

| Folder | What |
|---|---|
| `clipflow/` | V2.0 workflow on LangGraph + LangChain: 18 nodes, 8 agents. Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). |
| `foundation/` | The base platform: tasks, separate worker processes, leases, human review and resume, status, delivered files. Design: [docs/PLATFORM_DESIGN.md](docs/PLATFORM_DESIGN.md) |
| `workflows/` | Workflows that run on the platform: `clip` (Clipflow behind an adapter), `ingest` (a replay or product footage into the material library), `report` (a model drafts a document, a person reviews it) and `echo` (a tiny one for tests) |
| `pilot/` | The working slice-mode prototype (`run_job.py`), to be wrapped by `clipflow/workers/pilot.py` |
| `config.yaml` | Paths, output spec and the rule library |
| `examples/` | Sample product and job files for local setup |

## Review materials

- [Business overview](短视频带货剪辑项目业务说明.docx)
- [V2.0 technical workflow](技术方案V2.0流程图续图.png)
- [Platform design in Chinese](docs/PLATFORM_DESIGN.zh.md) and [acceptance record](docs/ACCEPTANCE_2026-11-27.md)

## Setup

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the gateway and database values. Never commit `.env`.

## Verification

The development workspace passed 219 tests. See [the acceptance record](docs/ACCEPTANCE_2026-11-27.md) for recovery, load and video checks. Test files are kept in the development workspace.

## Run a real job

Copy a sample job and product from `examples/` to local `jobs/` and `products/`, then set their paths to available media.

```powershell
.\.venv\Scripts\python.exe -m clipflow.run jobs\示例裤A-001.yaml
```

Answers each pause in the terminal (`--auto` for a scripted smoke test). With Postgres running, `--stop-at-pause` exits at the first pause and `--thread <id>` picks the run up later, from any process. A first run on a transcribed replay takes about 6 minutes; a re-run with caches under a minute. It prints the time of each step.

## HTTP API

```powershell
.\.venv\Scripts\python.exe -m clipflow.api
```

Serves `/api/v1/runs` on http://127.0.0.1:8000; interactive docs at http://127.0.0.1:8000/docs.

## Workbench (phase 3)

```powershell
.\.venv\Scripts\python.exe -m foundation.db migrate
.\.venv\Scripts\python.exe -m foundation.dev          # API + one worker per type; open http://127.0.0.1:8100/
```

The site opens on the platform's home: what waits for you, the apps, and the platform's state. Video
editing is one app (带货视频剪辑: new clip, register material, 素材库, its tasks) beside 文档起草; apps are
listed in `foundation/registry.py` (`APPS`), and the platform's name comes from `FOUNDATION_BRAND` in `.env`.

The look follows Yuxi's web client (github.com/xerrors/Yuxi, MIT): a sidebar with 新建任务, the pages and
the recent tasks (those waiting for you first), and each task shown as a conversation with the agent: your
request on the right, its steps as collapsible rows, its questions to you as approval cards, its files at the end.

Pages, in Chinese for the editors: the task list (filter "待审核" for what waits on you), a form for a new
clip task (slice or material mode), and a task page with progress, the review screens and downloads:

- **确认分镜**: the shots with their lines and captions; must-fix items in red, waivable ones in yellow.
  Approve, reject, or edit (change a caption, pick an alternate clip, delete a shot) and submit the edits.
- **审核成片**: the rendered video to watch, the QC results and the same shot table.
- **找不到商品**: give a rough time range in the replay, or cancel.
- **审核文档** (`report`): approve, edit the text, write comments and have it rewritten, or reject;
  the approved text downloads as Markdown.

- **确认入库片段** (`ingest`): where a replay sells which product, with the anchor's own words; play
  each from its start, correct times or products, drop or add segments, then confirm.
- **素材库**: pick a product and get every processed segment of it, with its file and time range.

Any other workflow gets a new-task form drawn from its input model and the generic review pages.

## Material library (phase 4)

```powershell
.\.venv\Scripts\python.exe -m clipflow.library migrate                 # the library's tables (schema `library`)
.\.venv\Scripts\python.exe -m workflows.ingest scan material            # register every new or changed file
.\.venv\Scripts\python.exe -m workflows.ingest scan material --every 600  # keep watching the folder
.\.venv\Scripts\python.exe -m clipflow.library find 示例裤A              # what the library has for a product
```

Or upload a file in the workbench (新建任务 → 素材入库 → 上传文件): it is sent in chunks and resumes after
a dropped connection. `material/<款名>/<颜色>/*.mov` is product footage; any other video is a replay, searched for every
product in `products/`. A replay's segments wait for a person to confirm them (确认入库片段); footage
is filed at once. Clip runs then use the confirmed segments and the tagged footage. Design: §14 of
[docs/PLATFORM_DESIGN.md](docs/PLATFORM_DESIGN.md).

**运行状态** shows each queue, which worker processes are alive, and plain warnings (e.g. clip tasks
queued but no clip worker running); the browser tab shows how many tasks wait for review. It also
checks disk space, the model gateway and the GPU every 5 minutes. `foundation.dev` restarts any process
that crashes. Old uploads: `.\.venv\Scripts\python.exe -m foundation.ops cleanup` lists them, `--apply`
removes them.

Database backup (keeps the newest 14; files are not included), and a check that a backup restores. On this
workstation the scheduled task `clipflow-backup` runs it every night at 02:00 (log: `backups/backup.log`).

```powershell
.\.venv\Scripts\python.exe -m foundation.ops backup
.\.venv\Scripts\python.exe -m foundation.ops restore-test backups\clipflow-db-<date>.dump
```

Put your name in "我的名字" (top right): it is recorded with every review. There is no login yet; the
server listens on this machine only. Login and access control come before editors use it from their
own computers.

## Platform (phase 1)

Postgres must be running (below). `migrate` creates or upgrades the tables; then two processes:

```powershell
.\.venv\Scripts\python.exe -m foundation.db migrate
.\.venv\Scripts\python.exe -m foundation.worker --types clip,echo
.\.venv\Scripts\python.exe -m foundation.api          # http://127.0.0.1:8100/docs
```

A clip task's input is a Clipflow `RunRequest` without `run_id` (the task id is used); it pauses at
`confirm_plan` and `review_video` (and at `needs_input` if the product isn't found in the replay).

- `POST /api/v1/tasks {"workflow_type": "echo", "input": {"text": "…", "review": true}}`, then `GET /api/v1/tasks/{id}`.
- A waiting task shows its question in `waiting_for`; answer with
  `POST /api/v1/tasks/{id}/decisions {"waitpoint_id": "…", "body": {"verdict": "approve", "version": 1}}`.
  An answer to an old question gives 409; one that doesn't fit gives 422.
- Tables live in their own Postgres schema (`foundation`, or `FOUNDATION_SCHEMA`); tests use a throwaway one.

## Database (for saved runs)

```powershell
docker compose up -d
```
```powershell
docker compose ps postgres
```
