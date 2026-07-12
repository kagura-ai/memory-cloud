# Memory Health Report (#1211)

`GET /api/v1/admin/memory-health` assembles the signals the system already
emits — Sleep reports, graph invariants, usage stats, search-config posture —
into one thresholded self-diagnosis document. The motivating failure class:
memory bugs that manifest as *plausible successes* (`sleep_summary.ok=true`
while every judge call failed, #1177). Each section grades `ok | warn | fail`;
`overall_status` is the worst section.

Scope (Phase 1): self-scoped — the report covers the calling admin's own data
partition, same discipline as the manual sleep trigger. Label-free signals
only; gold-label rates (`stale_only`, P@k) belong to the eval CI gates
([eval README](../../backend/tests/eval/README.md), #1210).

Web UI: **Admin → Memory Health** renders the same document.

## Grading philosophy

- **FAIL** fires only on deterministic facts (latest run failed, invariant
  violated). A false FAIL erodes dashboard trust.
- **WARN** covers degradation signals worth a look but not proof of breakage.
- Signals that exist only in logs (e.g. `reinforce_rerank_applied`) are
  excluded until persisted — never rendered as "pending".

## Sections, metrics, thresholds

### consolidation

Window: the 20 most recent Sleep reports for the user.

| Condition | Grade | Rationale |
|---|---|---|
| Latest sleep run `status=failed` | **fail** | Total judge death — the #1177 class. |
| Any `llm_call_failures > 0` or `degraded` run in the window | warn | Partial judge death grades `degraded`, never a silent `completed` (#1183). |
| `deferred_pairs > 0` in the window | warn | Cluster caps deferring candidate pairs unjudged — dedup may be structurally behind (#1184 class). |
| Oldest `sleep_maintenance` soft-deleted merge loser > 90 days | warn | Backlog suggests a retention window is wanted (`sleep_merge_retention_days`, #1209). |
| No sleep runs at all | ok | Sleep is opt-in (#558); absence is not failure. |

Metrics: `reports_in_window`, `latest_status`, `llm_call_failures`,
`degraded_runs`, `failed_runs`, `winner_overrides`, `deferred_pairs`,
`merge_backlog_count`, `merge_backlog_oldest_days`.

### graph

| Condition | Grade | Rationale |
|---|---|---|
| Any edge weight outside `[0.0, 3.0]` | **fail** | Deterministic invariant violation — the #1197 unclamped-accumulation class. |
| Zero edges with ≥ 25 active memories | warn | The graph never warmed; check edge-formation gates / `sleep_mode`. |

Metrics: `edges_by_origin` (hebbian / semantic / declared), `total_edges`,
`weight_violations`, `active_memories`, `edges_per_memory`.

### retrieval

Window: 7 days of `mcp:recall` / `mcp:remember` / `mcp:explore` usage.

| Condition | Grade | Rationale |
|---|---|---|
| Zero `recall()` calls with > 0 active memories | warn | The store is write-only — memory exists but nothing reads it. |

Metrics: `recall_calls`, `remember_calls`, `explore_calls`, `window_days`,
plus config posture (`contexts_with_config`, `reinforce_enabled`,
`use_rerank`).

## Relationship to the eval CI gates (#1210)

This report is the *runtime* half of the post-eval hardening pair: it grades
what production emits, continuously and label-free. The nightly eval workflow
grades what a frozen labeled corpus proves (update-correctness, placebo
compounding). A regression can trip either first; neither replaces the other.
