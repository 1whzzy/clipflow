# clipflow architecture (V2.0 scaffold)

Product information + footage in, a finished vertical selling video out, with two human checkpoints. This document is for reviewing the structure. Business background is in the requirements spec (`短视频带货剪辑助手AI平台V2.0详细需求规格说明书.pdf`).

**Status:** the structure is complete and both modes have produced real videos. **Open questions for the architect are in section 12.**

| | How it was checked | Result |
|---|---|---|
| Slice mode, real | One hour of a real replay of the pilot product (trousers), LLM relay, `clipflow.run` | 1080×1920, 30 fps, −14.1 LUFS MP4, QC passed |
| Material mode, real | A final script + 35 product shots; Windows' built-in voice standing in for TTS | 18.5 s MP4, −14.5 LUFS, QC passed |
| HTTP API, real | `clipflow.api` + plain HTTP: submit → plan pause → video pause → MP4 | Works |
| Every branch, loop, pause and review-gate rule | 80 tests on fakes built from the real run (desensitized) | Pass |
| Resume after a failure | Test: a step fails, the cause is fixed, the run continues from that step | Pass |
| Resume across processes, real | Postgres 18 in Docker: process A runs the real job to the plan pause and exits; process B resumes it by thread id (`--thread`) and finishes it; plus `tests/test_postgres.py` | Works: B ran only the remaining steps (render, QC); nothing before the pause re-ran |
| Lint | `ruff check clipflow tests` | Clean |
| Speed | Per-step timing in `clipflow.run` | First run on a transcribed replay ~6 min (frame tagging ~2.5, planning ~2.5, word timestamps < 1, render ~10 s); cached re-run < 1 min; a new replay adds ~8 min per hour for transcription |
| **Not yet** | | Output quality against a golden set (metrics in `clipflow/evaluate.py`, cases not collected yet); the real TTS service; the case library |

## 1. Three layers

| Layer | What it does | Where |
|---|---|---|
| Graph (LangGraph) | Order, branches, loops, human pauses, checkpoints | `clipflow/graph/` |
| Nodes | Thin step functions: read artifact ids from state, call an agent, a worker or plain code, store the result | `clipflow/nodes/` |
| Agents (LangChain) | The judgement steps: one input contract in, one output contract out, checked by code | `clipflow/agents/`, `clipflow/prompts/` |

Supporting modules: `contracts/` (Pydantic models for everything passed between steps), `rules.py` (the rule library), `llm/` (model access by tier), `workers/` (media work behind interfaces), `storage/` (artifact store and checkpointer).

## 2. The 18 nodes

```
intake → route_mode ─┬─ slice_front: transcribe → locate_product ⟲ → refine_words → look → select_quotes
                     └─ prepare_script
→ storyboard → match_assets → auto_review ⟲(1 rework, best version kept) → confirm_plan ⏸ → [tts] → build_timeline
→ render → qc ⟲(≤2) → review_video ⏸ ⟲(≤5 edit rounds) → deliver
```

The full diagram, including the subgraph, is generated from the code: `docs/workflow.mmd`.

| # | Node | Kind | Agent |
|---|---|---|---|
| 1 | intake | code: validates the request before anything costs money | – |
| 2 | route_mode | code + branch: flags missing category rules and an unverified price | – |
| 3 | transcribe | worker (GPU) | – |
| 4 | locate_product | agent + pause when not found | 商品定位 |
| 5 | refine_words | worker (GPU): word timestamps, only inside the blocks | – |
| 6 | look | agent; drops blocks that show another item | 画面理解 |
| 7 | select_quotes | agent | 脚本生成 (slice task) |
| 8 | prepare_script | agent | 脚本生成 (material task) |
| 9 | storyboard | agents → **Plan** | 卖点匹配, 分镜生成 |
| 10 | match_assets | code filter + agent ranking | 素材召回 |
| 11 | auto_review | rules in code, then agent; verdict computed in code | 合规审核 |
| 12 | confirm_plan | human pause (can be turned off per run) | – |
| 13 | tts | worker (material mode) | – |
| 14 | build_timeline | code, pure function: Plan → **Timeline** | – |
| 15 | render | worker (ffmpeg) | – |
| 16 | qc | code: size, fps, loudness, length, sync | – |
| 17 | review_video | human pause (mandatory) | – |
| 18 | deliver | code | – |
| – | (offline) | daily batch, not in the graph | 反馈归因 |

**Only the slice front is a subgraph.** It is specific to one mode, linear, and has its own loop ("not found → ask for a time range"). The shared stages stay in the main graph, so the review, QC and edit loops are plain edges you can read in one picture. A subgraph per stage would push those loops across subgraph boundaries.

## 3. The seam: Plan vs Timeline

- **`Plan`** is editorial: what is said and shown, in which order, and why. LLMs write it, code checks it, people confirm and edit it.
- **`Timeline`** is mechanical: tracks, in/out points in ms, captions, end card. Only code writes it, as a pure function of Plan + request + voice track, so it can always be rebuilt.
- Everything before Plan depends on the mode; everything after is shared.
- Review edits are **patches to Plan**, which create a new Plan version. Text edits go back through `auto_review`; picture and cut edits go straight to `build_timeline`.
- A reworked Plan **replaces** the old one; it is never merged. This avoids the V2.0 tech plan's `merge_shots` problem, where a 9-shot plan revised to 7 shots kept shots 8–9.

## 4. The 8 agents and the agent template

Six agents come from the V2.0 tech plan, chapter 5. Two are new, because a finished video needs the product found in the replay (商品定位) and the picture understood (画面理解). Every agent follows `agents/base.py`:

```
input contract → prompt (six-part outline, versioned file) + model (answers through one tool = the schema)
  → Pydantic parse → repair (local fixes)
  → check   (integrity: the output is wrong, e.g. a quote not in the transcript, an unknown line number)
  → quality (rules an editor could accept breaking, e.g. a quote under 1.2 s, a point said three times)
  → any problem? ask again with all problems listed (≤ 3 attempts)
  → out of attempts: best attempt without integrity problems → returned, quality problems as warnings
                     no such attempt                       → AgentFailed
```

- **Two kinds of check, so style can't stop a run.** An agent that keeps missing a style rule returns its best usable attempt with warnings, which reach the reviewer as `<node>_warning:…` flags at both pauses. Output that is *wrong* (integrity) is never accepted. Each agent's rules are split this way; for example, `prepare_script` treats rewording a final script as integrity, but an over-long line as quality.

- **The model never writes text it could get wrong.** For example, select_quotes answers with line numbers and code copies the anchor's words; locate's evidence must appear word for word in the transcript; storyboard's lines and durations are copied from its input.
- **The retry loop is plain Python, not a subgraph.** Attempts are seconds apart and cheap, so checkpointing each one buys nothing. What happens after the loop gives up is a workflow decision and lives in the node or graph:
  - `look`, `match_assets`, `auto_review`: continue degraded, flagged.
  - `locate_product`: ask the user.
  - The rest: the run fails and can be resumed from its last checkpoint (`clipflow.run --thread <id>`; the steps already done are not repeated).
- **The graph retries transient failures only** (`build.transient`): timeouts, dropped connections, rate limits, 5xx from the relay. A failed agent is not retried again by the graph: it already asked 3 times with its problems listed, and LangGraph's default would have repeated that up to 9 calls in total. Bad input, a missing file or an ffmpeg error fail the same way twice, so they aren't retried either.
- **Independent calls run in parallel** (`Agent.run_many`, at most `Limits.model_concurrency` = 6 at a time): frame batches, transcript chunks, footage ranking. Results keep input order; one failure doesn't affect the others. This cut frame tagging from 13.1 to 2.4 minutes.
- **Long inputs are split and sent compactly.** Transcripts and frame tags go as one text line per item, not JSON (about half the tokens). locate_product asks about a 1-hour transcript in chunks of 600 lines that overlap by 30, so a product talked about across a chunk boundary is seen whole at least once; blocks found twice are merged.
- **Thin inputs degrade instead of failing:** no word timestamps for the blocks → the line-level transcript is used (`no_word_timestamps` flag); no frames → picture checks are skipped (`no_frames` flag).
- **Agents don't call tools in v1.** Nodes fetch the data and pass it in. That keeps every input inside the cache key, so runs are reproducible. A tool whitelist becomes worth building when an agent has to search, for example locate over a full 4-hour transcript.
- **Caching:** the key is agent + prompt version + model + input. That makes resume and rework cheap: re-running a node after a pause costs no model call, and a rework only re-asks agents whose input changed.
- **Prompts:** `clipflow/prompts/<id>.v<N>.md`, with a YAML header (tier, owner, eval set, change note) and the six fixed sections. V1.0's prompt content is reused. Examples may only come from adopted real runs, never invented ones. A change is a new version, and the old one stays: the highest version is used, and every trace entry records which one. `select_quotes.v2` and `compliance.v2` came from two real runs (section 5).

## 5. Rules in code, not in prompts

`rules.py` reads `config.yaml`:
- `rules`: from the editor SOP, the V1.0 review checklist and the review of the first pilot video.
- `selling_point_priority`: from V1.0 node 6.

Banned words, prices, unverified claims, hook-first, shot variety and caption length are checked in code. The compliance agent only looks for what rules can't see. The final verdict is computed in code, so the model can never overrule a rule hit.

**Quote rules from the real runs.** Two cold runs of the pilot job showed the same problems every time, so they became code checks (`SelectQuotesAgent.quality`, limits in `config.yaml`) and the `select_quotes.v2` prompt:

| Seen in both runs | Rule |
|---|---|
| Shots of 0.6–1.0 s that sound cut off; 10–13 shots in 30 s | each quote ≥ 1.2 s (`min_quote_ms`), at most 8 quotes |
| Sizes said three times; 5 selling points | at most 1 main + 2 supporting points, the same point at most twice |
| The hook 「…喇叭裤只是说我现在穿的这个码有点太小了」 undercuts itself | no hedge after the hook (`opening_hedges`: 只是说, 但是, 不过 …) |
| The ending 「我们先上这个裤子吧」, 「再开100单」, 「旁边同事穿的是黑色」 | no live-room-only lines (`live_room_phrases`); the prompt also excludes people off screen |
| After those rules: one 13 s stretch repeating 「我正常穿的是M码」 twice; 「你看见了吗」 with nothing to see | each quote ≤ 8 s (`max_quote_ms`); no clause repeated inside a quote; "look" phrases (`deictic`) only where the frames show the item clearly |
| Middle quotes starting 「而且…」 「然后…」, which sound cut mid-sentence | no quote starts with a connective, not only the first |
| Acceptance run 2026-11-27: 「…它是发糯的这是第一点」, 「我觉得就是你就是去搭配一些那种…你像这种大派克」 | the anchor's numbering is cut (`list_markers`); more rambling fillers in `hesitations` |

**Video stage, from the same acceptance run.** The end card used to follow the last shot as a 3 s frozen, silent frame; it now lies over the last 2.2 s of the final shot (the anchor still speaking) with only a 0.8 s still after it (`END_CARD_MS`, `END_HOLD_MS` in `nodes/video.py`). For products worn below the waist (category containing 裤, 裙, 鞋, 袜, 靴) captions always go at the top, so they never cover the product (`caption_position`). Replay ingestion now also fetches word timestamps for the selling stretches it files, so a later clip run's `refine_words` step finds them cached (130 s → under 1 s on the acceptance job).

Measured on the same job (same replay, two cold runs before, then after each change):

| | Before (2 runs) | After (last runs) |
|---|---|---|
| Hook | 「…喇叭裤只是说我现在穿的这个码有点太小了」 both times | 「不是特别贴腿的喇叭裤」 (1.4 s) |
| Ending | 「我们先上这个裤子吧」 both times | 「…超级巨无霸显瘦，一整套都可以穿」 |
| Quotes in ~30 s | 10–13, several under 1.2 s | 6, from 1.4 to 7.3 s |
| Live-room lines, people off screen | 「再开100单」, 「旁边同事」 | none |
| First review: must-fix items | 7–8, so every plan was reworked | 1–3 real guarantees (「不会有任何鼓鼓囊塞」); none after one rework |

**Banned words: ordinals are not claims, and a hit is reworked, not fatal.** A real run was stopped by 「第一」 in 「这是第一点」 ("this is the first point"). Ordinals and counting (`banned_word_exceptions`: 第一点, 第一个, 第一次 …) are now excluded. A real banned word is must-fix, not an immediate stop: in slice mode the system picked that line, so one rework can pick another; after that it blocks approval and delivery until a person fixes it. `hard_fail` remains for findings no rework can fix (`block` severity, e.g. a category the platform doesn't allow); no current rule produces one.

**Deterministic repairs instead of re-asking.** Where a model's slip has one right correction, code makes it: priorities are renumbered, an unknown colour in a frame tag becomes "not visible", and evidence like 「rule:priority.其他：显瘦属于核心属性…」 is reduced to the id it starts with (a real run lost an attempt to that). Invented ids are still rejected.

**Review calibration.** The first review flagged 7–8 "must fix" items on both runs, mostly the anchor's subjective feel (「比打底裤舒服得多」, 「发糯」), so every plan was sent back. `compliance.v2` keeps `fix` for what would mislead a purchase (absolute guarantees, medical hints, unverifiable scarcity such as 「再开100单」, contradictions with the product sheet) and makes subjective praise and general comparisons `note`s for the reviewer.

The test suite shows this on real data. The prototype's 33 s video ended with 「…来169」 while `price_verified` was false. The rules flag that line, the review sends the plan back once, and then it reaches the human with the flag `review_unresolved`. The human can't approve it as is (section 6): the shot has to be deleted or the price verified first. The end card never shows an unverified price.

## 6. The review gate (both human pauses)

`nodes/_human.py` holds one rule set for `confirm_plan` and `review_video`. A submitted decision is checked before the run moves on. If it fails a check, the run pauses again with the error, so it never continues on a decision nobody meant.

- **Version:** a decision names the Plan or Timeline version the reviewer saw. A stale version is refused.
- **Valid edits:** shots must exist; a reorder lists every remaining shot once; you can't delete every shot.
- **Must-fix vs. waivable** (`rules.MUST_FIX`):
  - Fact and compliance errors can't be approved, only fixed or rejected: banned words, wrong or unverified prices, unverified material claims, invented caption words. The pilot's target for these in delivered videos is zero. They get one automatic rework first, like other findings.
  - Other findings (structure, shot variety, the compliance agent's style findings) can be waived, but only with a written reason. The reason is stored in the `Delivery`.
- **One automatic rework, then the best version.** On the real runs the first rework removed most problems (7 → 0, 6 → 1), and the second never helped (once it went 1 → 4). So `auto_review` reworks at most once (`Limits.review_rounds`), remembers the best agent-made version (fewest must-fix problems, then fewest others), and hands *that* one to the reviewer. A second rework would also cost about a minute.
- **Human edits are never overwritten:** after a person edits the plan, findings go back to that person, not to an agent. An automatic rework would rebuild the plan and lose the edits.
- **QC failures have an owner** (`QCCheck.fix_by`):
  - `system`: rebuild and re-render, at most 2 times.
  - `human`: e.g. a shot without footage. Blocks approval until fixed.
  - `admin`, or `system` after its retries: the run ends as `failed`. An admin can resume it from its last checkpoint.
- **`deliver` re-checks the gate.** A video with an open must-fix problem is never delivered.
- **Audio travels with each shot** (`Shot.speech`), so deleting or reordering shots can't give one shot another shot's voice.

## 7. Product entry and the ingestion boundary

A job can name just the product (`product_id` = 款名). `intake` loads the product sheet from the catalog. For a slice job without a replay, intake uses the replays ingestion linked to the product, and refuses the job if there are none. `locate_product` first asks the library for blocks ingestion already attributed and a person confirmed; only if there are none does the 商品定位 agent search the transcript. When nothing is found, the user can type a rough range like `1:02:30`.

The interface is `workers/base.py: Catalog` and `AssetLibrary` (`replays_for`, `linked_blocks`, `find`). In the pilot, `products/*.yaml` and the prototype's tags of the product shots stand in for it.

## 8. HTTP API

`clipflow/api.py` (FastAPI) is how a front end drives runs and the two pauses, following the spec's `/api/v1/runs`:

| Call | Does |
|---|---|
| `POST /api/v1/runs` | Submit a job (a `RunRequest`) → `202 {run_id}`. The run executes in a background thread. |
| `GET /api/v1/runs/{id}` | Where it is: `running`, `waiting` (with the pause payload: kind, version, actions, must-fix and waivable findings, last error), `done`, `failed` (with the error and the failed step). |
| `POST /api/v1/runs/{id}/resume` | Answer the pause. An invalid answer is not an HTTP error: the run pauses again and `GET` shows why. |
| `POST /api/v1/runs/{id}/continue` | After a technical failure is fixed, run on from the failed step. |

Each run is one LangGraph thread (`thread_id = run_id`), so with the Postgres checkpointer any API process can answer any run. Verified with real workers: submit → plan pause → video pause → MP4, over HTTP.

## 9. State, artifacts, checkpoints

- **State** (`graph/state.py`) holds ids, counters and flags only. Every field has exactly one writer node. The two append-only lists (`flags`, `trace`) have an explicit reducer.
- **Artifacts** (`storage/artifacts.py`) are immutable JSON files, with the id `<kind>/<content hash>`. Local disk for the pilot; the interface is what an S3-compatible store would implement.
- **Checkpointer** (`storage/checkpoint.py`): Postgres in production (`docker-compose.yml`), in-memory in tests.
- **Pauses** use `interrupt()`. A node re-runs from the top on resume, so nothing before the pause has side effects. The one agent call before a pause (`locate_product`) is cached, which makes re-running it free.

## 10. Long-running work

ASR (about 8 min per hour of footage), frame sampling, TTS and rendering sit behind the interfaces in `workers/base.py`. Today they run in-process (`workers/pilot.py`, which reuses the prototype's transcription, frame extraction and caption code; rendering goes straight from the Timeline, with per-shot parts cached for partial re-renders). The same interface can later submit jobs to a GPU worker queue and wait, without changing the graph.

## 11. Deliberately left out (for now)

| Left out | Why |
|---|---|
| Milvus, Elasticsearch, Kafka, MongoDB, Redis, Kubernetes | One person, one workstation. Locating a product works without a vector DB (LLM over the transcript + vision). Add them when a measured need appears, e.g. cross-replay search. |
| Dual-run and cut-over from Coze | V1.0 never ran in production; nothing to migrate from. |
| LangSmith tracing | It would send prompts and product data to an outside service. `trace` in state records prompt version, attempts, tokens and cache hits per agent call instead. |
| Event stream (SSE) | The API is poll-based (`GET /runs/{id}`). An event stream for live progress is the next step once a front end exists. |
| Ingestion (detect new replays and shots, transcribe, tag, index) | A separate pipeline on its own schedule that fills the asset library the graph reads. |

## 12. Open questions for the architect

1. **Ingestion boundary.** Is `product id → linked replay blocks → shot candidates` (`AssetLibrary`) the right interface? Who attributes a replay block to a product, and when is that "confirmed"? What happens to a block whose product is unclear: does the run wait for ingestion to resolve it, or does `locate_product` decide and flag it?
2. **User preferences and cases.** Today `options.focus` and `hook_style` reach quote picking, selling points and the case search; cases are looked up in `storyboard`, after the words are chosen, by category, price, focus and hook style. Should cases also shape the words, as in V2.0's parallel retrieval (cases ∥ script)? That decides what the case library stores and how it is queried.
3. **Must-fix list.** Is `rules.MUST_FIX` the right line between "a person may waive with a reason" and "must be fixed"?
4. **TTS** for material mode: which service (on-premise vs. API)? Today `workers/pilot.py: WindowsTTS` (Windows' built-in zh-CN voice) stands in, to prove the path.
5. **Long jobs.** Keep ASR and rendering in-process for the pilot, or a worker queue from the start?

## 13. Fixes to the V2.0 tech plan carried in this design

1. Joins wait for all inputs (`add_edge([a, b], c)`); there are no parallel branches yet.
2. A reworked Plan replaces the old one (no `merge_shots`).
3. Slice mode never falls back to material mode for lack of product footage.
4. A rework goes back to where the problem was made: content (7/8), storyboard (9) or footage (10). It doesn't always go to the storyboard.
5. The video stage is mandatory human review. There is no 24-hour auto-approve.

## 14. Running it

```powershell
.\.venv\Scripts\python.exe -m clipflow.run jobs\示例裤A-001.yaml
```

- The tests cover both modes, every loop, every pause and each review-gate rule, on fakes.
- `python -m clipflow.evaluate <golden.yaml>` scores stored runs against an editor's choices (found rate, overlap; spec 8.2). Sample: `examples/eval/golden.yaml`.
- `python -m ruff check clipflow` lints; `python -m clipflow.graph.build` regenerates `docs/workflow.mmd` (a test fails if it is out of date).
- `python -m clipflow.api` serves the HTTP API on http://127.0.0.1:8000 (interactive docs at `/docs`).
- `clipflow.run` runs one job on real footage with the LLM relay. Pauses are answered in the terminal; `--auto` is a scripted reviewer for smoke tests.
