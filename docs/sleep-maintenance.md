# Sleep Maintenance

> **LLM Knowledge Base mapping**: Sleep Maintenance is Kagura's batch implementation of the **Compile** + **Enhance** layers from the [LLM Knowledge Base 5-layer pattern](architecture.md#llm-knowledge-base--5-layer-mapping). Where MCP `remember()` does continuous micro-compile per fact, Sleep Maintenance does periodic whole-context consolidation — the "wiki rewrite" step in Karpathy's terminology, executed as a background cron job rather than an on-demand agent task.

Sleep Maintenance is Kagura Memory Cloud's background cleanup cycle: a nightly, per-context batch process that discovers missing edges, merges duplicates, re-evaluates importance, consolidates working memories, re-indexes what changed, and records the entire run for auditing and rollback.

Write paths (`remember`, chunking, edge creation) optimize for ingest speed. Over time this leaves debt — near-duplicates, stale importance values, memories that should have been promoted or archived, and graph gaps between semantically related notes. Sleep is where that debt is paid down asynchronously so reads (`recall`, `explore`) stay precise.

## Execution Flow

```
┌─────────────────────────────────────────────────────────────┐
│  APScheduler  (SLEEP_CRON_HOUR, default 02:00 UTC)          │
│    guards: ENABLE_NEURAL_MEMORY=true AND SLEEP_ENABLED=true │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  sleep_maintenance_task()                                    │
│    iterates distinct (user_id, workspace_id, context_id)    │
│    from Memory table (deleted_at IS NULL)                   │
└─────────────────────────────────────────────────────────────┘
                              ↓ per context
┌─────────────────────────────────────────────────────────────┐
│  SleepOrchestrator.run()                                     │
│    1. Load NeuralMemoryConfig                                │
│    2. Resolve context.sleep_mode → full / edges_only / skip │
│    3. Create SleepReport (status=running)                    │
│    4. Execute phases with shared SleepBudget                 │
│    5. Finalize report (completed | failed | rolled_back)    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌──────────┬──────────┬───────────┬─────────────┬──────────┐
│ Phase 1  │ Phase 2  │  Phase 3  │   Phase 4   │ Phase 5  │
│  Edge    │  Dedup   │ Importance│Consolidation│ Reindex  │
│Discovery │  / Merge │  Re-eval  │             │          │
└──────────┴──────────┴───────────┴─────────────┴──────────┘
                              ↓
                      ┌───────────────┐
                      │   Phase 6     │
                      │   Report      │
                      │  (audit log)  │
                      └───────────────┘
```

Phases are independently recoverable. If phase N fails, phases N+1..6 still execute, the error is recorded in the report, and the run is marked `completed` with the failing phase's error captured — fatal exceptions mark the run `failed`. Budget (`SleepBudget`) is shared across all phases and enforces upper bounds on LLM calls and memories processed per run.

## The Six Phases

### Phase 1 — Edge Discovery

Finds missing edges between related memories using medium-similarity Qdrant search with optional LLM judgment.

- **Algorithm**: recency-weighted sampling of edge-poor memories → medium-similarity neighbor search (0.6–0.9) → drop pairs that already have edges → LLM batch judgment for `related`, `edge_type`, `confidence` → create confirmed edges via `NeuralEdgeRepository`.
- **LLM**: yes (optional — skipped if the LLM service is disabled).
- **Notes**: positional bias is mitigated by shuffling batch order; short labels (A, B, C) in prompts prevent ID hallucination.
- **Offline evaluation**: see [`docs/eval/edge_discovery_labeling.md`](eval/edge_discovery_labeling.md) for the labeling protocol used to measure judge correctness on labeled pairs (complementary to production observability metrics).

### Phase 2 — Dedup / Merge

Detects and merges duplicate memories by clustering high-similarity neighbors.

- **Algorithm**: per-memory high-similarity search → build candidate pairs → Union-Find clustering (cluster size capped at 5 to prevent runaway merges) → LLM batch judgment (`merge` / `keep_both`) in LLM mode, or auto-merge at similarity ≥ 0.98 in non-LLM mode → keep winner, soft-delete losers, transfer edges, merge tags.
- **LLM**: yes (optional).
- **Side effects**: soft-deletes losers in PostgreSQL and removes them from Qdrant.

### Phase 3 — Importance Re-evaluation

Adjusts memory importance using LLM scoring combined with EMA smoothing.

- **Algorithm**: target memories with importance ∈ [0.2, 0.8] and staleness ≥ 7 days → LLM batch score → `new = α · llm_score + (1 − α) · old`, with α defaulting to `importance_ema_alpha = 0.3` → clamp to [0.0, 1.0] → update PostgreSQL and Qdrant payloads.
- **LLM**: yes.
- **Why EMA**: a single LLM call can shift importance by at most 30%, protecting against one bad judgment destroying a memory's weight. At daily cadence ~7 runs reach 95% convergence.

### Phase 4 — Consolidation

Promotes, keeps, or archives working memories based on fast-path rules plus LLM judgment for borderline cases.

- **Algorithm**: rule-based fast path for clear-cut `promote` / `archive` decisions (no LLM) → LLM judgment only for borderline cases → bridge-node protection (never delete memories with high graph centrality).
- **LLM**: yes (optional — in non-LLM mode, behavior matches the legacy `consolidation_task`).
- **Replaces**: the pre-Sleep rule-only `consolidation_task`.

### Phase 5 — Reindex

Re-embeds and upserts memories whose text or metadata was changed by earlier phases, keeping Qdrant in sync with PostgreSQL.

- **Algorithm**: collect `changed_memory_ids` from phases 1–4 → batch fetch from PostgreSQL → regenerate embeddings via `EmbeddingService` → upsert Qdrant vectors and payloads.
- **LLM**: no.
- **Always runs** when there are changes, regardless of which earlier phases ran.

### Phase 6 — Report

Finalizes the `SleepReport` row with aggregated per-phase results (edges created, memories merged, promoted, flagged), cost counters (LLM calls, tokens, embedding calls), and overall status. Per-action audit entries written by earlier phases to `sleep_actions` become the rollback log.

## `sleep_mode` Configuration

Each context has a `sleep_mode` column (`Context.sleep_mode`, default `"full"`) that determines which phases run for that context.

### Changing `sleep_mode`

End users can change the mode via the REST API or the Web UI.

**API:**

```bash
curl -X PUT http://localhost:8080/api/v1/contexts/{context_id} \
  -H "Content-Type: application/json" \
  -H "Cookie: session=..." \
  -d '{"sleep_mode": "skip"}'
```

Valid values: `"full"`, `"edges_only"`, `"skip"`. Changing `sleep_mode` requires **owner** access (editor will receive 403).

**Web UI:**

Navigate to a context's **Settings** tab → **Sleep Maintenance** card. Select the desired mode from the dropdown. Choosing `skip` shows a confirmation dialog because it stops all automated maintenance until re-enabled.

### Modes

| Mode          | Phases                                              | Typical use                                              |
|---------------|-----------------------------------------------------|----------------------------------------------------------|
| `full`        | Edge Discovery → Dedup → Importance → Consolidation → Reindex → Report | Default. Interactive memory contexts.                    |
| `edges_only`  | Edge Discovery → Reindex → Report                   | Resource-ingested contexts (external docs, ref material). |
| `skip`        | — (the run is aborted before any phase starts)      | Externally managed / large-scale contexts where Sleep is unwanted. |

If a context's `sleep_mode` is changed while a Sleep run is in progress for that context, the in-flight run is **not** interrupted; the new mode takes effect on the next scheduled run.

Mode resolution happens in `SleepOrchestrator._get_sleep_mode()`. Unknown or missing contexts fall back to `"full"`. The Reindex and Report phases themselves are not gated by `sleep_mode`; they run whenever at least one preceding phase executed and produced changes.

## LLM Service Layer

Phases 1–4 call the LLM through `LLMService`, which supports:

- **Providers**: OpenAI (default) and Ollama via its OpenAI-compatible endpoint. Ollama connectivity is health-checked at startup against `ollama_base_url`. Configured via `SLEEP_LLM_PROVIDER` and `SLEEP_LLM_MODEL` environment variables (or the corresponding fields in Neural Config).
- **API key priority** (most specific wins): context-scoped key → workspace-scoped key → user-scoped key → `OPENAI_API_KEY` env var fallback.
- **Interface**: `complete_json()` returns `(parsed_json, tokens_used)`; each phase aggregates tokens into its `PhaseResult`, and the reporter rolls them up into the `SleepReport`.
- **Budget enforcement**: every phase checks `SleepBudget.can_afford()` before issuing an LLM batch. When the budget is exhausted, later phases are skipped with `skip_reason="budget_exhausted"`. Defaults: `max_llm_calls=50`, `max_memories=200` per run (override via Neural Config).

## Scheduling

Sleep runs as a cron-triggered APScheduler job registered in `schedule_sleep_tasks()`.

| Env var               | Default       | Purpose                                                |
|-----------------------|---------------|--------------------------------------------------------|
| `SLEEP_ENABLED`       | `false`       | Must be `true` for the job to be registered and run.   |
| `ENABLE_NEURAL_MEMORY`| `false`       | Must be `true`; the task aborts early otherwise.       |
| `SLEEP_CRON_HOUR`     | `2`           | Hour of day (UTC) at which the job fires.              |
| `SLEEP_CRON_MINUTE`   | `0`           | Minute of hour at which the job fires.                 |

At trigger time, the task loads `NeuralMemoryConfig` once, enumerates distinct `(user_id, workspace_id, context_id)` tuples from the `Memory` table (excluding soft-deleted rows), and invokes `SleepOrchestrator.run()` per tuple. Each context's run is committed independently; a failure in one context rolls back only that transaction and continues with the next.

## Observability & Rollback

Every phase writes individual change records to `sleep_actions` during execution, which the reporter aggregates into a `SleepReport` row. Both are exposed through MCP tools and admin REST endpoints.

### MCP tools

| Tool                  | Purpose                                                                  |
|-----------------------|--------------------------------------------------------------------------|
| `get_sleep_history`   | List recent sleep runs for a context with status, timing, and counters.  |
| `get_sleep_report`    | Fetch a single report with per-phase results and the full action audit log. |
| `rollback_sleep_run`  | Reverse every recorded action of a run in reverse order.                 |

### Admin REST endpoints

| Endpoint                                    | Purpose                                                   |
|---------------------------------------------|-----------------------------------------------------------|
| `GET /admin/sleep-reports`                  | List reports with filters (`status`, `context_id`, `user_id`) and pagination. |
| `GET /admin/sleep-reports/{report_id}`      | Report detail with the full action audit log.             |

Both admin endpoints require `Depends(require_admin)`.

### Report status values

```
running  →  completed | failed | cancelled | rolled_back
```

### Rollback semantics

`rollback_sleep_run` walks the `sleep_actions` log in reverse and reverses each recorded change by type:

- **edge_created** — delete the created edge.
- **memory_merged** — restore the soft-deleted loser(s), revert winner's merged tags and content.
- **importance_updated** — restore the previous importance value.
- **memory_promoted** — revert the promotion.
- **memory_archived** — restore from archive.

Restored memories are re-embedded back into Qdrant. On full success the report moves to `rolled_back`; if any action fails to reverse, the report is marked `failed` and the partial state is recorded in the audit log.

## Admin UI

- **Sleep Reports list + detail** — `frontend/src/app/(authenticated)/admin/sleep-reports/` renders the `/admin/sleep-reports` REST endpoints as a browsable report explorer with per-action drilldown.
- **Neural Config — Sleep category** — `frontend/src/app/(authenticated)/admin/neural-config/` exposes the tuning knobs listed below without requiring an env-var redeploy.

## Tuning Knobs

All Sleep-specific settings live under the Sleep category of Neural Config (`backend/src/api/routes/neural_config.py`) and are persisted in the database. Env vars of the same name act as bootstrap defaults.

| Field                         | Purpose                                                        |
|-------------------------------|----------------------------------------------------------------|
| `sleep_llm_provider`          | `openai` or `ollama`.                                          |
| `sleep_llm_model`             | Model identifier passed to the provider.                       |
| `sleep_max_memories_per_run`  | Upper bound on memories touched per context per run.           |
| `sleep_max_llm_calls_per_run` | Upper bound on LLM calls per context per run (budget cap).     |
| `sleep_dedup_enabled`         | Master toggle for Phase 2.                                     |
| `sleep_edge_discovery_enabled`| Master toggle for Phase 1.                                     |
| `sleep_importance_reeval_enabled` | Master toggle for Phase 3.                                 |
| `sleep_consolidation_enabled` | Master toggle for Phase 4.                                     |

Per-phase toggles apply on top of `sleep_mode`: a phase runs only if `sleep_mode` allows it *and* the phase's enable flag is `true`.

## Related

- [Architecture](architecture.md) — Service Layer and overall system context.
- [Core Concepts](concepts.md) — Workspace, Context, and Memory primitives that Sleep operates on.
- GitHub: issues #101, #103 (original design), #164 (observability + rollback), #178 / #179 (admin UI).
