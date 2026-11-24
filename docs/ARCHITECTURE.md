# clipflow architecture (V2.0 scaffold)

Product information + footage in, a finished vertical selling video out, with two human checkpoints. This document is for reviewing the structure. Business background is in the requirements spec (`短视频带货剪辑助手AI平台V2.0详细需求规格说明书.pdf`).

**Status:** the structure is complete. It runs end to end in tests (fake models and media workers) and on real footage with the LLM relay: the pilot product (trousers), one hour of a real replay → a 1080×1920, 30 fps, −14 LUFS MP4 that passes QC (`python -m clipflow.run`, section 13). A first run on an already-transcribed replay takes about 6 minutes: frame tagging about 2.5 min (44 vision calls, 6 at a time), planning about 2.5 min, word timestamps under 1 min, rendering about 10 s. A cached re-run takes under a minute. Transcribing a new replay adds about 8 min per hour of footage. Not done yet: TTS for material mode (no service chosen), the case library, and the Postgres checkpointer on this machine (it needs Docker). **Open questions for the architect are in section 11.**

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
  → Pydantic parse → repair (local fixes) → check (business rules in code)
  → problems? ask again with the problems listed (≤ 3 attempts) → still failing: AgentFailed
```

- **The model never writes text it could get wrong.** For example, select_quotes answers with line numbers and code copies the anchor's words; locate's evidence must appear word for word in the transcript; storyboard's lines and durations are copied from its input.
- **The retry loop is plain Python, not a subgraph.** Attempts are seconds apart and cheap, so checkpointing each one buys nothing. What happens after the loop gives up is a workflow decision and lives in the node or graph:
  - `look`, `match_assets`, `auto_review`: continue degraded, flagged.
  - `locate_product`: ask the user.
  - The rest: the run fails and can be resumed from its last checkpoint.
- **Independent calls run in parallel** (`Agent.run_many`, at most `Limits.model_concurrency` = 6 at a time): frame batches, transcript chunks, footage ranking. Results keep input order; one failure doesn't affect the others. This cut frame tagging from 13.1 to 2.4 minutes.
- **Long inputs are split and sent compactly.** Transcripts and frame tags go as one text line per item, not JSON (about half the tokens). locate_product asks about a 1-hour transcript in chunks of 600 lines, each cached, and merges the blocks.
- **Agents don't call tools in v1.** Nodes fetch the data and pass it in. That keeps every input inside the cache key, so runs are reproducible. A tool whitelist becomes worth building when an agent has to search, for example locate over a full 4-hour transcript.
- **Caching:** the key is agent + prompt version + model + input. That makes resume and rework cheap: re-running a node after a pause costs no model call, and a rework only re-asks agents whose input changed.
- **Prompts:** `clipflow/prompts/<id>.v<N>.md`, with a YAML header (tier, owner, eval set, change note) and the six fixed sections. V1.0's prompt content is reused. Examples may only come from adopted real runs, never invented ones.

## 5. Rules in code, not in prompts

`rules.py` reads `config.yaml`:
- `rules`: from the editor SOP, the V1.0 review checklist and the review of the first pilot video.
- `selling_point_priority`: from V1.0 node 6.

Banned words, prices, unverified claims, hook-first, shot variety and caption length are checked in code. The compliance agent only looks for what rules can't see. The final verdict is computed in code, so the model can never overrule a rule hit.

The test suite shows this on real data. The prototype's 33 s video ended with 「…来169」 while `price_verified` was false. The rules flag that line, the review sends the plan back once, and then it reaches the human with the flag `review_unresolved`. The human can't approve it as is (section 6): the shot has to be deleted or the price verified first. The end card never shows an unverified price.

## 6. The review gate (both human pauses)

`nodes/_human.py` holds one rule set for `confirm_plan` and `review_video`. A submitted decision is checked before the run moves on. If it fails a check, the run pauses again with the error, so it never continues on a decision nobody meant.

- **Version:** a decision names the Plan or Timeline version the reviewer saw. A stale version is refused.
- **Valid edits:** shots must exist; a reorder lists every remaining shot once; you can't delete every shot.
- **Must-fix vs. waivable** (`rules.MUST_FIX`):
  - Fact and compliance errors can't be approved, only fixed or rejected: banned words, wrong or unverified prices, unverified material claims, invented caption words. The pilot's target for these in delivered videos is zero.
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

## 8. State, artifacts, checkpoints

- **State** (`graph/state.py`) holds ids, counters and flags only. Every field has exactly one writer node. The two append-only lists (`flags`, `trace`) have an explicit reducer.
- **Artifacts** (`storage/artifacts.py`) are immutable JSON files, with the id `<kind>/<content hash>`. Local disk for the pilot; the interface is what an S3-compatible store would implement.
- **Checkpointer** (`storage/checkpoint.py`): Postgres in production (`docker-compose.yml`, test with `pilot/scripts/check_db.py`), in-memory in tests.
- **Pauses** use `interrupt()`. A node re-runs from the top on resume, so nothing before the pause has side effects. The one agent call before a pause (`locate_product`) is cached, which makes re-running it free.

## 9. Long-running work

ASR (about 8 min per hour of footage), frame sampling, TTS and rendering sit behind the interfaces in `workers/base.py`. Today they run in-process (`workers/pilot.py`, which reuses the prototype's transcription, frame extraction and caption code; rendering goes straight from the Timeline, with per-shot parts cached for partial re-renders). The same interface can later submit jobs to a GPU worker queue and wait, without changing the graph.

## 10. Deliberately left out (for now)

| Left out | Why |
|---|---|
| Milvus, Elasticsearch, Kafka, MongoDB, Redis, Kubernetes | One person, one workstation. Locating a product works without a vector DB (LLM over the transcript + vision). Add them when a measured need appears, e.g. cross-replay search. |
| Dual-run and cut-over from Coze | V1.0 never ran in production; nothing to migrate from. |
| LangSmith tracing | It would send prompts and product data to an outside service. `trace` in state records prompt version, attempts, tokens and cache hits per agent call instead. |
| API/SSE layer (FastAPI) | Next, once the graph is reviewed. It wraps `compile_app` (`POST /runs`, `/resume`, `/state`). |
| Ingestion (detect new replays and shots, transcribe, tag, index) | A separate pipeline on its own schedule that fills the asset library the graph reads. |

## 11. Open questions for the architect

1. **Ingestion boundary.** Is `product id → linked replay blocks → shot candidates` (`AssetLibrary`) the right interface? Who attributes a replay block to a product, and when is that "confirmed"? What happens to a block whose product is unclear: does the run wait for ingestion to resolve it, or does `locate_product` decide and flag it?
2. **User preferences and cases.** Today `options.focus` and `hook_style` reach quote picking, selling points and the case search; cases are looked up in `storyboard`, after the words are chosen, by category, price, focus and hook style. Should cases also shape the words, as in V2.0's parallel retrieval (cases ∥ script)? That decides what the case library stores and how it is queried.
3. **Must-fix list.** Is `rules.MUST_FIX` the right line between "a person may waive with a reason" and "must be fixed"?
4. **TTS** for material mode: which service (on-premise vs. API)?
5. **Long jobs.** Keep ASR and rendering in-process for the pilot, or a worker queue from the start?

## 12. Fixes to the V2.0 tech plan carried in this design

1. Joins wait for all inputs (`add_edge([a, b], c)`); there are no parallel branches yet.
2. A reworked Plan replaces the old one (no `merge_shots`).
3. Slice mode never falls back to material mode for lack of product footage.
4. A rework goes back to where the problem was made: content (7/8), storyboard (9) or footage (10). It doesn't always go to the storyboard.
5. The video stage is mandatory human review. There is no 24-hour auto-approve.

## 13. Running it

See the [README](../README.md) for installation and the CLI entry point. References to tests above describe verification in the development workspace.
