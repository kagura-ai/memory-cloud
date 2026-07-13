# Memory Health Report (#1211, #1225)

`GET /api/v1/admin/memory-health` assembles the signals the system already
emits — Sleep reports, graph invariants, usage stats, search-config posture —
into thresholded self-diagnosis documents. The motivating failure class:
memory bugs that manifest as *plausible successes* (`sleep_summary.ok=true`
while every judge call failed, #1177). Each section grades `ok | warn | fail`;
each document's `overall_status` is the worst section.

Scoping (#1225, Phase 2): the report is broken down **per context**, so a
warning always names the context it came from.

- `GET /api/v1/admin/memory-health` — the breakdown: one graded entry per
  owned context (`context_id`, display name, `overall_status`, per-section
  statuses). No metric is summed across contexts; the page-level
  `overall_status` is the worst entry. A user with zero contexts gets `ok`
  with an empty list.
- `GET /api/v1/admin/memory-health?context_id=<uuid>` — the 3-section
  detailed document for that single context. Ownership is validated
  (`created_by` = caller, not deleted); anything else is a uniform 404.
- `GET /api/v1/admin/memory-health?context_id=unattributed` — the detail
  document for signals that do not belong to an owned, live context:
  recorded **without** a `context_id` (account-wide sleep runs, legacy
  rows), or under a **soft-deleted** context, or under a **shared context
  created by another member**. These fold into one explicit "unattributed"
  breakdown entry instead of being silently dropped — dropping them would
  hide Phase-1 warnings behind the new grouping. The unattributed entry
  skips the cold-graph and write-only heuristics (it mixes scopes, so both
  would be noise) while still grading deterministic facts like failed
  sleep runs and edge-weight violations.

Everything stays self-scoped (the calling admin's own data partition, same
discipline as the manual sleep trigger). Workspace-level rollup across
members is deliberately Phase 3. Label-free signals only; gold-label rates
(`stale_only`, P@k) belong to the eval CI gates
([eval README](../../backend/tests/eval/README.md), #1210).

Web UI: **Admin → Memory Health** renders the breakdown as a status list
with drill-down into the per-context detail.

## Grading philosophy

- **FAIL** fires only on deterministic facts (latest run failed, invariant
  violated). A false FAIL erodes dashboard trust.
- **WARN** covers degradation signals worth a look but not proof of breakage.
- Signals that exist only in logs (e.g. `reinforce_rerank_applied`) are
  excluded until persisted — never rendered as "pending".
- **Isolation contract** (#1225): grading reads only the target context's
  rows — a WARN-producing signal in context A cannot change context B's
  grade (pinned by tests).

## Sections, metrics, thresholds

### consolidation

Window: the 20 most recent Sleep reports **per context**, bounded to the
last **180 days**. The recency bound keeps the window scan cheap as history
grows and stops ancient failures in sparse contexts from resurfacing as
eternal WARNs (the Phase-1 account-wide window had already aged them out).

| Condition | Grade | Note code | Rationale |
|---|---|---|---|
| Latest sleep run `status=failed` | **fail** | `latest_sleep_failed` | Total judge death — the #1177 class. |
| Any `llm_call_failures > 0` in the window | warn | `judge_failures` | Partial judge death grades `degraded`, never a silent `completed` (#1183). |
| A `degraded` run with zero judge failures | warn | `degraded_runs` | A maintenance phase crashed (graded degraded by #1229's phase-failure rule); the run's error message names the phase. |
| A `failed` run in the window with a recovered latest | warn | `failed_runs_recovered` | Recent instability is worth a look even after recovery. |
| `deferred_pairs > 0` in the window | warn | `deferred_pairs` | Cluster caps deferring candidate pairs unjudged — dedup may be structurally behind (#1184 class). |
| Oldest `sleep_maintenance` soft-deleted merge loser > 90 days | warn | `merge_backlog_old` | Backlog suggests a retention window is wanted (`sleep_merge_retention_days`, #1209). |
| No sleep runs at all | ok | — | Sleep is opt-in (#558); absence is not failure. |

Metrics: `reports_in_window`, `latest_status`, `llm_call_failures`,
`degraded_runs`, `failed_runs`, `winner_overrides`, `deferred_pairs`,
`merge_backlog_count`, `merge_backlog_oldest_days`.

### graph

| Condition | Grade | Note code | Rationale |
|---|---|---|---|
| Any edge weight outside `[0.0, 3.0]` | **fail** | `edge_weight_violations` | Deterministic invariant violation — the #1197 unclamped-accumulation class. |
| Zero edges with ≥ 25 active memories | warn | `cold_graph` | The graph never warmed; check edge-formation gates / `sleep_mode`. Skipped for the unattributed entry. |

Metrics: `edges_by_origin` (hebbian / semantic / declared), `total_edges`,
`weight_violations`, `active_memories`, `edges_per_memory`.

### retrieval

Window: 7 days of `mcp:recall` / `mcp:remember` / `mcp:explore` usage,
attributed per context.

| Condition | Grade | Note code | Rationale |
|---|---|---|---|
| Zero `recall()` calls with > 0 active memories | warn | `write_only_store` | The store is write-only — memory exists but nothing reads it. Skipped for the unattributed entry. |

Metrics: `recall_calls`, `recall_upcoming_calls`, `remember_calls`,
`explore_calls`, `window_days`, plus config posture (`has_config`,
`reinforce_enabled`, `use_rerank` — booleans per context).

Attribution caveat: a cross-context `recall(context_ids=[...])` is logged
under the **first** listed context only, so read activity on the other
listed contexts is invisible to this section. A `write_only_store` WARN on
a context that is only read via cross-context recall is a known false
positive until usage logging attributes every listed context.

## Structured notes (#1225)

Section notes are structured records — `{"code": "<note_code>", "params":
{...}}` — not prose. The frontend maps codes to localized strings
(`en.json` / `ja.json` under `admin.memoryHealth.notes`) and interpolates
the params; an unknown code renders a generic localized fallback (never a
crash, never a blank). GitHub issue references never appear in the payload
or the rendered UI — the note-code → design-rationale mapping in the tables
above is the deep link for operators.

| Note code | Params |
|---|---|
| `latest_sleep_failed` | — |
| `judge_failures` | `count`, `degraded_runs` (only runs whose own judge failed — phase-crash degraded runs are counted by `degraded_runs` instead) |
| `degraded_runs` | `count` |
| `failed_runs_recovered` | `count` |
| `deferred_pairs` | `count` |
| `merge_backlog_old` | `oldest_days`, `threshold_days` |
| `edge_weight_violations` | `count`, `min`, `max` |
| `cold_graph` | `active_memories` |
| `write_only_store` | `window_days`, `active_memories` |

Adding a note code is a three-place change: the service emits it, both
message catalogs localize it, and the table above documents it.

## Relationship to the eval CI gates (#1210)

This report is the *runtime* half of the post-eval hardening pair: it grades
what production emits, continuously and label-free. The nightly eval workflow
grades what a frozen labeled corpus proves (update-correctness, placebo
compounding). A regression can trip either first; neither replaces the other.
