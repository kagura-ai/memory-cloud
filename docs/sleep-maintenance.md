# Sleep Maintenance

> **LLM Knowledge Base mapping**: Sleep Maintenance is Kagura's batch implementation of the **Compile** + **Enhance** layers from the [LLM Knowledge Base 5-layer pattern](architecture.md#llm-knowledge-base--5-layer-mapping). Where MCP `remember()` does continuous micro-compile per fact, Sleep Maintenance does periodic whole-context consolidation — the "wiki rewrite" step in Karpathy's terminology, executed as a background cron job rather than an on-demand agent task.

Sleep Maintenance is Kagura Memory Cloud's background cleanup cycle: a nightly, per-context batch process that discovers missing edges, merges duplicates, re-evaluates importance, consolidates working memories, re-indexes what changed, and records the entire run for auditing and rollback.

Write paths (`remember`, chunking, edge creation) optimize for ingest speed. Over time this leaves debt — near-duplicates, stale importance values, memories that should have been promoted, and graph gaps between semantically related notes. Sleep is where that debt is paid down asynchronously so reads (`recall`, `explore`) stay precise.

**A memory is kept until its owner or writer deletes it** (#3021). `forget`, a duplicate merge the deployment enabled (Phase 2) and the documented retention of deleted rows (Phase 2.5, the platform cleanup sweep) are the only ways a row goes away. No phase archives or deletes a memory for age or disuse, and neither does the legacy `consolidation_task` that runs when Sleep is off.

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
│    4. Execute phases, each with its own SleepBudget         │
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

Phase 4.5 (Supersede Apply, #3020) runs between Phase 4 and Phase 5 only when the deployment switches it on (`sleep_supersede_apply_enabled`, off by default).

Phases are independently recoverable. If phase N fails, phases N+1..6 still execute, the error is recorded in the report, and the run is marked `degraded` with the failing phase named — fatal exceptions mark the run `failed` (see "Report status values" below). Each phase has its own budget (`SleepBudget`, #3019): the full memory allowance and a share of the run's LLM calls, so one phase cannot starve another (see "Budget enforcement" below).

## The Six Phases

### Phase 1 — Edge Discovery

Finds missing edges between related memories using medium-similarity Qdrant search with optional LLM judgment. **Off by default** (`sleep_edge_discovery_enabled`, #3021): its edges are read by `explore` and the opt-in related index.

- **Algorithm**: recency-weighted sampling of edge-poor memories → medium-similarity neighbor search (0.6–0.9) → drop pairs that already have edges → LLM batch judgment for `related`, `edge_type`, `confidence` → create confirmed edges via `NeuralEdgeRepository`.
- **LLM**: yes (optional — skipped if the LLM service is disabled).
- **Notes**: positional bias is mitigated by shuffling batch order; short labels (A, B, C) in prompts prevent ID hallucination.
- **Offline evaluation**: see [`docs/eval/edge_discovery_labeling.md`](eval/edge_discovery_labeling.md) for the labeling protocol used to measure judge correctness on labeled pairs (complementary to production observability metrics).

### Phase 2 — Dedup / Merge

Detects and merges duplicate memories by clustering high-similarity neighbors.

- **Compared with the whole context** (#3019): each memory of the night's window is searched against the whole partition — the run's user's memories in the context — not only against the window. A hit outside the window is compared when it passes the window's own exclusions (pinned rows, tool guardrails, time memories, rows the resource indexer owns, forgotten rows) and its last embedding succeeded; its full row and stored vector are read only when its cluster is about to be judged, and the direct-score check below applies to it as to a window row. A new memory is therefore compared with an old duplicate the night after it is written, and a memory of any age can lose a merge to a newly written duplicate. In a shared context a memory is compared only with the same user's memories: a merge never tombstones another member's memory. Which memories form the window: see [Coverage](#coverage-3019).

- **Algorithm**: per-memory high-similarity search → build candidate pairs → Union-Find clustering (cluster size capped at 5 to prevent runaway merges) → LLM batch judgment (`merge` / `keep_both`) in LLM mode, or auto-merge at similarity ≥ 0.98 in non-LLM mode → deterministic winner rules → keep winner, soft-delete losers, transfer edges, merge tags.
- **Deterministic winner rules** (applied to every LLM decision; the duplicate verdict is trusted, the direction is enforced): **newer wins** (#1198 — the recency-aware veto: the judge alone sometimes kept the older version of an updated fact), and at **equal recency, human-authored wins** (#1209 — a `source_type='manual'` memory never loses to ingested content on a tie). Overrides are counted in the report (`winner_overrides`, with a per-rule `winner_override_reasons` breakdown).
- **What "newer" means** (#3019): a memory's recency is the later of its creation time and the time of the last edit that changed its `summary` or `content`, read from the revision log (`memory_revisions`). `updated_at` is not the recency: writes that change no text stamp it too (a merge winner's tag union, the undo of a promotion, a context-merge copy, and importance re-evaluation before it kept its own clock), so an old memory stamped that way does not outrank a newer duplicate. Only an edit of `summary` or `content` counts: an edit of tags, importance, type, `details` or `context_summary` alone does not make a memory newer. When such a memory loses a merge, only its tags are carried into the winner (they are unioned); its own `details`, `context_summary`, type and importance stay with the soft-deleted loser, which `undo_merge_action` / `rollback_sleep_run` restore while the merge retention allows. A session rollback that puts older text back takes away the edits it reverted, and one that changed no text keeps them. A memory at version 1 is compared by its creation time without reading the log; for the others, one indexed read per judged cluster. The judge's prompt shows this value as `last_updated=`, the rules compare it at minute precision, the rule path uses it to break an importance tie, and the merge audit records it (`winner_recency` / `loser_recency`).
- **When the log cannot tell, both stay** (#3019): the revision log exists only since the deployment ran v0.113.0, so a text edit made before that is not in it, and a memory edited then still reads as never edited. Its `updated_at` may be that edit or a bookkeeping stamp; nothing tells which. A merge is therefore not made, in either direction, when the memory that would lose has no logged text edit, its `updated_at` is later than the other memory's recency, and the log began after that recency (otherwise an edit later than it would be in the log). When the log began is read once a run, as the time of its first row; a deployment with no row yet treats every such pair this way. A memory whose revision read failed, and one a session rollback naming no reverted block put older text back on, are kept the same way when their stamp (or the latest edit that rollback may have restored) is later than the other memory's recency. A pair the rule path decides on importance is not affected. The rule applies to the judge, the rule path and shadow mode (no `supersedes` edge), pair by pair: the rest of the cluster still merges, and since every merge that is still made has a winner at least as recent as its loser, or on the rule path one that outranks it on importance, a kept memory cannot be merged away through a third memory. A cluster in which no judge answer could merge anything is not sent to the judge. Each kept pair counts in `no_action_reasons.ambiguous_recency`; its memories count as looked at for the lanes ([Coverage](#coverage-3019)), and the pair is compared again when the sweep's next pass or a write to either memory brings it back (kept pairs are not remembered, like a judge's `keep_both`). Pairs of memories older than the log stay unmerged this way whenever the one that would lose carries a later `updated_at`, including the stamp an earlier Sleep merge put on its winner. **Limit**: a context-merge copy starts at version 1 with a fresh `updated_at` and none of its source's log, so an edit made in the source context before the merge is not seen; once the log began before the other memory's recency, the copy is compared by its creation time and can lose to a duplicate written between its creation and that edit.
- **Pinned memories are never candidates** (#1519, #1523): rows with `delivery_mode='always'` — the deterministic `load_pinned()` lane (#886) — are exempt from every automated pass through one shared predicate (`not_pinned_predicate()` in `models/memory.py`): dedup merge, consolidation and importance re-evaluation all exclude them at their candidate fetch, and `rollback_sleep_run` refuses to demote a pinned promotion. In dedup they can be neither a merge loser (which would silently drop a standing guardrail out of `load_pinned()`) nor a winner (merge folds only tags, so a pin "winning" would discard the newer unpinned fact instead of absorbing it). A vector-search hit outside the window is considered only under the same exclusions (and the merge's locked re-check refuses a row pinned, forgotten, moved to another context or edited since; an edit while the judge ran moves its version, and the pair counts in `no_action_reasons.changed_since_fetch`), so a pinned neighbour cannot re-enter that way either; the per-memory neighbour cap is widened by the number of pinned rows in scope (bounded) so pinned near-duplicates cannot crowd out an unpinned memory's true duplicate. An unpinned near-duplicate of a pinned memory is therefore left alone; the ingest-time `supersede_candidate` hint remains the surface that points it out. **Upgrading an install that already ran dedup:** the exclusion is write-side only — a pinned row that lost a merge before this rule stays soft-deleted (restore it with `undo_merge_action` while `sleep_merge_retention_days` allows), and in shadow mode a `supersedes` edge already written against a pinned row keeps hiding it from `recall()` until the edge is removed. Audit `sleep_actions` merge rows whose loser had `delivery_mode='always'`.
- **Time memories are never candidates** (#1524): rows with `type='time'` — the deterministic `recall_upcoming()` lane, served by `details.trigger`, not by summary — are excluded at the same candidate fetch. Two occurrences of a recurring window ("standup 09:00" on different dates) share a summary and embed identically, and merge folds only tags, so the loser's window would silently vanish from the lane. Same upgrade note applies: the exclusion is write-side only; audit merge rows whose loser had `type='time'`.
- **Resource-indexed rows are never candidates** (#2996): a row the resource indexer owns (it carries a resource's `resource_id`, `doc_id` and numeric `version` and names the indexer's point; `resource_owned_predicate()` in `models/memory.py`, the one rule every automated pass applies) belongs to its source, which alone replaces or removes it. Dedup merge, consolidation and importance re-evaluation exclude it at their candidate fetch, and the writes check again (the merge's locked re-check, whose refusals are counted in the report's `skipped_resource_owned`, and the importance UPDATE); the legacy `consolidation_task` does not promote it (counted in its `resource_skipped`). This holds whatever the context's settings: a sync target that left feed management (manual mode) with `sleep_mode='full'` keeps its rows. Such rows still answer the vector search, so they widen dedup's per-memory neighbour cap like pinned rows do. The indexer writes these rows `persistent` from creation (and makes a working row it takes over persistent); migration `f62_2996_resource_rows_persist` moved the ones written `working` before v0.181.0.
- **Open memos never lose a merge** (#2584): a pair whose loser carries one of `SLEEP_ARCHIVE_EXEMPT_TAGS` (exact match, default `status:open`) is skipped in both modes and counted as `skipped_exempt`. It is not flipped: merge folds only tags into the winner, so an exempt winner would discard the other side's (often newer) content — the reason pinned memories are excluded rather than made absolute winners. An exempt memory can still win and absorb a non-exempt duplicate; two exempt memories never merge, and their pair is judged again on later runs. The tag is read from the candidate fetch (no execution-time re-check, unlike a pin).
- **Direct-score eligibility** (#1229, #1231): a merge decision only executes when the (winner, loser) pair's **direct** pairwise similarity is at/above the dedup threshold. Union-Find clusters chain transitively, so the judge can otherwise nominate two members whose true pairwise similarity is far below the threshold — merging distinct facts that merely share a template. The per-memory top-10 neighbor search saturates in dense duplicate clusters (#1231), so before judging, missing in-cluster pairwise scores are completed locally from the summary vectors already embedded during candidate discovery (≤10 cosines per cluster; the judge — LLM prompt and rule-based path alike — sees the true score instead of 0.0, and a genuinely duplicate pair merges instead of being false-positive vetoed). Report counters: `llm_merge_guarded` (sub-threshold vetoes only), `llm_merge_rescued` (merges on pairs the candidate cap had missed), `llm_merge_unverifiable` (no score computable — vector unavailable; fail-closed veto), `settled_decisions_skipped` (#1232 — shadow-mode judge re-nominations of pairs a supersedes edge already settled: skipped, never re-merged, never counted as guarded).
- **Audit** (#1209): every merge writes a `sleep_actions` entry carrying `merge_reason` (the judge's rationale, or the rule id on the non-LLM path), override info, both sides' `source_type` provenance and recency keys — every merge is explainable and its decision inputs are snapshotted.
- **LLM**: yes (optional).
- **Side effects**: soft-deletes losers in PostgreSQL and removes them from Qdrant (vectors are rebuilt on undo/rollback). A `supersedes` link [Phase 4.5](#phase-45--supersede-apply-3020) wrote at a loser, at either end, is deleted rather than moved to the winner (#3020): it was decided for that pair only.

### Phase 2.5 — Merge Retention (#1209)

Hard-deletes merge losers whose soft-deletion is older than the declared retention window — destructive deletion as an explicit, telemetered second step, never a side effect of the merge itself. The consolidation archives earlier versions made (`deleted_by='sleep_consolidation'`, #1520; nothing archives since #3021) share this window — both sleep tombstone classes are one set (`SLEEP_TOMBSTONE_DELETED_BY`), and the user-forget window is defined as its complement.

- **Default**: `sleep_merge_retention_days = 0` — **no additional Sleep purge**. This is not "retain forever": the platform-wide cleanup sweep (`CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`, default 30 days, `0` disables it) still hard-deletes every tombstone older than its window, whether or not Sleep runs and regardless of who tombstoned the row (#1521). The same Sleep window bounds the consolidation archives earlier versions made. Undo and rollback are possible inside whichever window is shorter.
- **When enabled**: losers past the window are purged and the run records one batch-summary `purge` action (`purged`, `retention_days`, `cutoff`). Per-merge undo and run rollback are only possible **inside** the window; the undo API returns 410 naming this setting once a loser is purged.
- **LLM**: no.

### Phase 3 — Importance Re-evaluation

Adjusts memory importance using LLM scoring combined with EMA smoothing. **Off by default** (`sleep_importance_reeval_enabled`, #3021): importance reaches `recall` only through an explicit filter and a small re-rank.

- **Algorithm**: target memories with importance ∈ [0.2, 0.8], neither edited nor re-evaluated in the last 7 days → LLM batch score → `new = α · llm_score + (1 − α) · old`, with α defaulting to `importance_ema_alpha = 0.3` → clamp to [0.0, 1.0] → update PostgreSQL and Qdrant payloads.
- **Order and bound** (#3019): never re-evaluated memories first (oldest first), then the ones re-evaluated longest ago, and no more than the phase's LLM calls can judge. When more were due, the phase's details say `more_due: true` and `budget_cut: true`; they come first next run, so a context is covered in a bounded number of runs.
- **Its own clock** (#3019): a re-evaluation writes `importance` and `importance_reevaluated_at`, never `updated_at`, so it is not an update: `changes_since` lists no `updated` event for it, and `list`'s order, `updated_after` / `updated_before`, dedup's newer-wins and the related index's evidence check do not see it. Every memory of a batch whose judge call answered is marked, whether or not its importance changed; a batch whose call failed marks nothing (its memories are due again next run). An embedding-model switch re-queues a memory re-evaluated after its re-embed started, so the new collection gets the new importance. **Upgrade note**: memories a re-evaluation stamped before this change keep that `updated_at` (there is no repair), so `list`'s order and `updated_after` / `updated_before` still see those stamps. Dedup's newer-wins does not take `updated_at` as recency (Phase 2, "What newer means"); such a stamp can only keep a pair unmerged when the revision log cannot tell it from an edit ("When the log cannot tell, both stay").
- **LLM**: yes.
- **Why EMA**: a single LLM call can shift importance by at most 30%, protecting against one bad judgment destroying a memory's weight. At daily cadence ~7 runs reach 95% convergence.

### Phase 4 — Consolidation

Promotes working memories to `persistent` by rule, and optionally by an LLM judge for the borderline ones. It archives and deletes nothing (#3021): a memory the rules do not promote stays `working`. `scope` has no effect on ranking, quota or retention: `persistent` means used, or old enough by rule; `working` is everything else.

- **Algorithm**: rule-based promotion (adoption via `reference()`, high importance once aged, graph centrality; no LLM) → the LLM judge decides `promote` vs `keep` for the memories no rule promoted, only when `sleep_consolidation_judge_enabled` is on (**off by default**) and a Sleep LLM is configured.
- **The judge continues where it stopped** (#3019): it sees the borderline memories in `(created_at, id)` order, starting after the memory it reached last run and wrapping, so when its calls do not cover them all, later runs reach the rest instead of judging the same ones every night. Which memories are promoted or kept is unchanged.
- **Report keys**: `rule_promoted`, `borderline`, `llm_promoted`; `rule_deleted`, `llm_archived` and `llm_archive_guarded` stay in the report and read 0, so older reports and their readers keep one shape. `rollback_sleep_run` still restores the archives (`deleted_by='sleep_consolidation'`) earlier versions made, while their tombstones survive.
- **Upgrade note** (#3021): `CONSOLIDATION_ADOPTION_DELETE_CUTOFF` is gone; nothing reads it. A row that used to be archived (30 days old, never adopted, after the cutoff) is now borderline: it stays working, or the judge, when on, may promote it. Rows archived or deleted before the upgrade are not restored by it (`rollback_sleep_run` restores a past run's archives while their tombstones last).
- **LLM**: optional (the judge); off by default.
- **Replaces**: the pre-Sleep rule-only `consolidation_task`, which still runs when `SLEEP_ENABLED` is not `true`; it promotes by its own rules and deletes nothing.

### Phase 4.5 — Supersede Apply (#3020)

Turns a `supersede_candidate` the deployment's judge verified into the `supersedes` link an acceptance would make, from the newer memory to the older one, and clears the suggestion. **Off by default**: the phase is part of a run only while `sleep_supersede_apply_enabled` is on (a deployment switch, see [Tuning Knobs](#tuning-knobs)), and then runs last in a `full` run, after Phase 4 and before the reindex. With the switch off a run is exactly what it was before: no statement, no report key, no coverage entry, no action.

- **Where it runs**: only where the run's user may write edges in the context, as `update_memory(accept_supersede_candidate=true)` requires (a workspace role of `member` or higher with a seat, and a writable context slot). Otherwise the phase is skipped with `skip_reason="not_writable"` before it reads any memory. It acts only on the run user's own memories. In a run whose judge calls all failed (the run grades `failed`, which `rollback_sleep_run` does not take) it is skipped with `skip_reason="run_failed"` and keeps its position, so every link it writes belongs to a run that can be rolled back.
- **What it applies**: a stored verdict `supersedes` at or above `SUPERSEDE_VERIFY_MIN_CONFIDENCE` (default 0.7, the floor at which recall marks a suggestion `verified: true`), still about the texts the judge saw, with the older memory's text not written after the newer one's. The judge sees each memory's summary up to 500 characters and the first 600 characters of its content, and the verdict covers those heads only: an older memory whose summary or content is longer is not linked. "Not written after" is dedup's recency ([Phase 2](#phase-2--dedup--merge)): the later of a memory's creation and its last text edit in the revision log, so an older memory corrected in place after the newer one was written is left alone, and so is a pair the log cannot order (the conditions under which dedup keeps both). The confidence of each verdict is recorded in its action.
- **What it never touches**: pinned memories, tool guardrails, time memories and memories the resource indexer owns (the memories dedup never compares), an older memory carrying an exempt tag (`SLEEP_ARCHIVE_EXEMPT_TAGS`) or belonging to another user; dismissed, `parallel`, `unclear`, unverified or stale suggestions; a pair that already has a `supersedes` or `contradicts` edge (either direction, any author) or a declared edge of the run user from the newer memory to the older one. An edge of another type that nobody declared (a Hebbian association, a discovered `related_to`) is retyped to `supersedes`, and its state is recorded so a rollback puts it back. Each memory read and not applied is counted in `no_action_reasons`: `malformed_candidate`, `below_floor`, `target_ineligible`, `newer_target`, `ambiguous_recency`, `verdict_stale`, `older_past_judged_head`, `already_linked`, `contradicts`, `declared_edge_conflict`, `changed_since_read` (something changed between the read and the write) or `write_failed`. The edges are read by the two memories alone, in any workspace or context, as recall reads them. These checks hold when the link is written: an older memory pinned, given an exempt tag or made a tool guardrail afterwards keeps its link until the edge is deleted or the run rolled back.
- **What it writes**: per pair, after checking both memories and the pair's edges again under the two memories' row locks, in a savepoint of its own: the `supersedes` edge (origin `semantic`, weight and confidence 1.0, metadata `{"source": "sleep_supersede_apply", "report_id": ...}`), the suggestion cleared (neither memory's `updated_at` nor `version` moves; nothing is re-embedded) and one `apply_supersede` action (`prior_candidate`, `prior_edge`, `verdict_confidence`, `similarity`). A write that fails counts under `write_failed` and grades the run `degraded`; the other pairs go on. The row locks are held until the run commits (through the reindex); an edge someone else writes on the pair after the re-check is not read again, so a `supersedes` link a person writes from the older memory to the newer one in that time stands next to the applied one.
- **Bound**: at most `sleep_max_memories_per_run` suggestions per run, oldest first, continuing from where the last run stopped ([Coverage](#coverage-3019)). The result is stored in `dedup_result.supersede_apply` (`applied`, `floor` and the three keys of Phase 6).
- **LLM**: no.
- **What else changes**: `changes_since` lists a `superseded` event for the pair, dated when the link was written, and the newer memory leaves the never-delivered list and count, as with an accepted link.
- **To reject one link**: delete the edge (`delete_edge(source_id=<newer>, target_id=<older>)`). A dismissal (`dismiss_supersede_candidate=true`) has no suggestion to act on once the link is applied. After the delete the suggestion does not come back by itself; a later edit of the newer memory can detect the pair again and, while the switch is on, a later run can apply it again: dismiss that new suggestion to stop it.
- **Rollback**: `rollback_sleep_run` removes the link (or puts back the edge it retyped) and restores the suggestion; Sleep then leaves that suggestion alone until the pair gets a new verdict (an edit of the newer memory's judged text and a new detection; a detection that reuses the stored verdict keeps the mark), so accept or dismiss it yourself. A link a person re-declared or another writer changed since stays (`supersede_links_kept`). Turning the switch off stops new links and leaves the applied ones until their run is rolled back. Before downgrading to a release without this phase after the switch was on, roll back the runs whose links should go: an older release skips `apply_supersede` actions, still marks the report `rolled_back` (so the run cannot be rolled back again), and reads every applied link as an ordinary `supersedes` link.
- **In recall**: an applied link does not hide. Recall and the user facts lane return the older memory as a row of its own with `superseded_by` (the newer memory's id), with or without the newer memory among the results, and nothing folds for the pair; the `load_guardrails` index and the related index keep it. Accepted and declared links, `remember(supersedes=)` and dedup's shadow-mode edges still hide; accepting or declaring the same link yourself (`create_edge(source_id=<the newer memory>, target_id=<the older memory>, edge_type="supersedes")`) makes it a declared link, which hides.

### Phase 5 — Reindex

Re-embeds and upserts memories whose text or metadata was changed by earlier phases, keeping Qdrant in sync with PostgreSQL.

- **Algorithm**: collect `changed_memory_ids` from phases 1–4 → batch fetch from PostgreSQL → regenerate embeddings via `EmbeddingService` → upsert Qdrant vectors and payloads.
- **LLM**: no.
- **Always runs** when there are changes, regardless of which earlier phases ran.
- **Failures** (#3036): a memory that cannot be re-indexed is counted under `failed` (its id in `failed_ids`) and the others go on. When some failed, the phase still counts as succeeded (no `error` on it; its `failed` count says how many) and the run is `degraded` with `reindex_partial_failure: N of M reindex operations failed`; when all failed, the phase fails and the run is `degraded` with `phase_failure: reindex`. A failed memory is re-indexed by the next run that changes it.

### Phase 6 — Report

Finalizes the `SleepReport` row with aggregated per-phase results (edges created, memories merged, promoted, flagged), cost counters (LLM calls, tokens, embedding calls), and overall status. Per-action audit entries written by earlier phases to `sleep_actions` become the rollback log.

- **Why a phase did nothing** (#3019): every phase that ran carries three keys in its `details`, early exits included: `candidates_considered` (what it looked at: memories for edge discovery, dedup, importance re-evaluation and consolidation; candidate edges for the related index; tombstones or observations past the cutoff for the retention phases; changed memories for the reindex), `no_action_reasons` (a count per reason for doing nothing, zero counts left out, so `{}` means nothing was left undone; the units differ per reason, so the counts do not add up to the candidates) and `budget_cut` (the phase stopped because its budget ran out, with work left). A skipped or failed phase has none of the three; its `details` are null, except that a dedup or consolidation judge that failed after reading its memories keeps `details.coverage` ([Coverage](#coverage-3019)). Existing keys and `message` strings are unchanged; consolidation's `llm_pass_cut_for_budget` equals its `budget_cut`.
- **Where each result is stored**: `edge_discovery_result` (with the related index under its `related_index` key), `dedup_result` (with the supersede-apply result under its `supersede_apply` key, #3020), `merge_retention_result` (with the `forget_retention` and `measurement_retention` results under those keys), `importance_result`, `consolidation_result`, `reindex_result`.
- **Budget-limited phases** (#3019): the run summary carries `budget_limited_phases`, `{phase: "skipped" | "cut"}` in run order (`{}` when none): `skipped` when the phase was not run because its budget was used up at entry (`skip_reason="budget_exhausted"`), `cut` when it ran and stopped with work left. It is derived from the stored results when a report is read (a report written before this key existed shows consolidation's `llm_pass_cut_for_budget`), and is returned by the REST report list and detail, by `get_sleep_history` rows and `get_sleep_report`, and marked in the web report list and detail.

## `sleep_mode` Configuration

Each context has a `sleep_mode` column (`Context.sleep_mode`, default `"skip"`) that determines which phases run for that context. New contexts are silent by default — Sleep Maintenance is opt-in (#558).

### Changing `sleep_mode`

End users can change the mode via the REST API or the Web UI.

**API:**

```bash
curl -X PUT http://localhost:8080/api/v1/contexts/{context_id} \
  -H "Content-Type: application/json" \
  -H "Cookie: kagura_session=..." \
  -d '{"sleep_mode": "skip"}'
```

Valid values: `"full"`, `"edges_only"`, `"skip"`. Changing `sleep_mode` requires **owner** access (editor will receive 403).

**Web UI:**

Navigate to a context's **Settings** tab → **Sleep Maintenance** card. Select the desired mode from the dropdown. Choosing `skip` shows a confirmation dialog because it stops all automated maintenance until re-enabled.

### Modes

| Mode          | Phases                                              | Typical use                                              |
|---------------|-----------------------------------------------------|----------------------------------------------------------|
| `full`        | Edge Discovery → Dedup → Importance → Consolidation → Reindex → Report | Interactive memory contexts (opt-in via owner Settings). |
| `edges_only`  | Edge Discovery (when `sleep_edge_discovery_enabled`) → Reindex → Report | Resource-ingested contexts (external docs, ref material). |
| `skip`        | — (the run is aborted before any phase starts)      | Default for new contexts; externally managed / large-scale contexts where Sleep is unwanted. |

If a context's `sleep_mode` is changed while a Sleep run is in progress for that context, the in-flight run is **not** interrupted; the new mode takes effect on the next scheduled run.

Mode resolution happens in `SleepOrchestrator._get_sleep_mode()`. Unknown or missing contexts fall back to `"skip"`. The Reindex and Report phases themselves are not gated by `sleep_mode`; they run whenever at least one preceding phase executed and produced changes.

## LLM Service Layer

Phases 1–4 call the LLM through `LLMService`, which supports:

- **Providers**: `openai` (default), `anthropic`, `gemini` and `self_hosted` (any OpenAI-compatible backend, e.g. Ollama, vLLM — `SELF_HOSTED_MODEL_ALIASES` rewrites the wire model id, a backend that rejects `response_format` gets one retry without it, request timeout `SELF_HOSTED_LLM_TIMEOUT_SECONDS`). Self-hosted connectivity is health-checked once per service instance against `self_hosted_base_url`.
- **Provider / model precedence** (#1569): a `neural_config` row (`sleep_llm_provider` / `sleep_llm_model`) > explicit `SLEEP_LLM_PROVIDER` / `SLEEP_LLM_MODEL` env > the deployment's `MANAGED_LLM_PROVIDER` / `MANAGED_LLM_MODEL` (logged once as `sleep_llm_from_managed_lane`) > the code default `openai` / `gpt-5-nano`. `SLEEP_LLM_FORCE_ENV=1` pins env over the DB row.
- **API key priority** (most specific wins): context-scoped key → workspace-scoped key → the provider's platform env var (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`; `SELF_HOSTED_BASE_URL` for `self_hosted`). With `RESOLVE_STORED_BYOK_KEYS=false` the two stored-key tiers are skipped, and a judge on the managed lane resolves the platform credential only (`platform_only`). See "LLM Credentials" in [deployment.md](deployment.md).
- **Interface**: `complete_json()` returns `(parsed_json, tokens_used)`; each phase aggregates tokens into its `PhaseResult`, and the reporter rolls them up into the `SleepReport`.
- **Budget enforcement** (#3019): each phase gets its own budget. Its memory allowance is the full `sleep_max_memories_per_run` (so a run's `memories_processed` can exceed it, one allowance per phase). The run's `sleep_max_llm_calls_per_run` is shared in equal parts among the enabled phases of the run that call a model — edge discovery and dedup with a Sleep LLM configured, importance re-evaluation, and the consolidation judge when it is on — in run order, the remainder of the division to the earliest; the calls a phase leaves unused roll forward to the next such phase, so the run never spends more than the setting. Every phase checks `SleepBudget.can_afford()` before an LLM batch. A model phase whose share is used up at entry is skipped with `skip_reason="budget_exhausted"` (the run stays `completed`); a phase that calls no model — the retention phases, the related index, and consolidation's rule pass — is never skipped for budget, and consolidation records `llm_pass_cut_for_budget` / `llm_unjudged` when its judge ran out of calls. `sleep_edge_discovery_sample_size` above the memory allowance is clamped at run time (log `sleep_edge_discovery_sample_clamped`); the configuration API accepts the pair. Defaults: `sleep_max_llm_calls_per_run=50`, `sleep_max_memories_per_run=200` (override via Neural Config).

### Coverage (#3019)

Every memory of a context is reached in a bounded number of runs, whatever the per-run allowance. A run works on a partition: the memories of one user in one context.

- **Dedup's window, two lanes.** The *changed lane* takes the memories written or edited since its position; when more changed than fit, the newest are taken and the rest is left to the sweep, which reads it within the current pass plus one. The *sweep* reads older memories in stable `(created_at, id)` order, newest first, and after the oldest starts a new pass. Memories that share a timestamp neither repeat nor get skipped.
- **The sweep's share.** A quarter of `sleep_max_memories_per_run` (at least 1) is reserved for the sweep, and so is a quarter of dedup's judge calls (at least 1); with an allowance or a call share of 1 the two lanes take turns from run to run. A context with many new memories cannot keep the sweep from reading older ones: on the rule path a pass over P memories takes at most about P / reserve runs, or three times that where the sweep keeps meeting memories of an oversize cluster that merges every run (below), and the next run counts it.
- **When a position moves.** Only past memories whose work is done in the run: judged, kept because the revision log cannot tell which of a pair is newer, or with nothing to judge. A memory whose own search or judge call failed while the same work succeeded for others is passed (the next pass reaches it again). For a failed embedding the evidence is another embedding the provider answered: one served from the cache does not count, and a refusal that is never one memory's own (the embedding spend cap, a missing provider key or plan) never passes a memory. So on a night when the embedding provider or the judge is down, or the spend cap is used up, the lane waits; after three runs held at its head it passes those memories and the report counts them (`skipped_after_stalls`, reason `failure`). A memory left for lack of calls is read again next run, however many runs that takes. So is a memory in an oversize cluster where a merge just ran: the pairs the split left out are compared once the merge has shrunk the cluster. When such a memory is the sweep's head, that run counts toward the same limit of three (a run held by a failure and one held by a merge count alike), because near-duplicates that keep arriving would otherwise run a merge in its cluster every night and hold the sweep for good: on the third run the memory is passed (reason `oversize_merge`), and the pairs left out for it wait until the changed lane or the sweep's next pass reaches it. The changed lane needs no such limit: a memory held there keeps no other one out. The changed lane's position never comes closer than ten minutes to the run's start, so a memory whose write was still committing is read again; a write that commits much later with an older timestamp is reached by the sweep.
- **Where the positions live.** On the run's report (`sleep_reports.coverage_state`: memory ids and timestamps, no text), written in the run's transaction and read under the graph learning lock at the start of the next run of the same user and context. A run that fails stores none: the next run continues from the run before. `rollback_sleep_run` and a per-merge undo leave the positions alone. The first run after the upgrade has none: its changed lane takes the newest memories, and each partition is read once over the following runs, within the nightly budget.
- **Supersede apply** keeps one sweep position (Phase 4.5, #3020): it reads the memories carrying a `supersedes` verdict in `(created_at, id)` order, oldest first, from that position, and after the last one starts a new pass. A memory it applied leaves the set; one it refused is read again on the next pass. A write that failed holds the position unless another pair of the run was applied, and is passed after three held runs like the lanes above. The entry is stored only once the phase has run, so a deployment that never switches it on stores the state it stored before.
- **The consolidation judge** keeps one position the same way (Phase 4). **Importance re-evaluation** needs none: it takes the memories judged longest ago first (Phase 3). **Edge discovery** still samples at random.
- **Moving memories between users** (`transfer_context_creator`): the receiving user's positions apply to the moved memories; the ones above its sweep position are read on the next pass.
- **In the report**: `dedup_result.details.coverage` says, per lane, how many memories it took and moved past (`taken`, `advanced`), whether the changed lane left a backlog to the sweep, how many memories it passed after a failure of their own (`passed_with_failure`), whether the sweep completed a pass (`pass_completed`, `passes`), and how many of the sweep's memories an oversize merge held in the run (`sweep.held_by_oversize_merge`); `skipped_after_stalls` counts the memories passed after three held runs and `skipped_after_stalls_reasons` what held them (`failure`, `oversize_merge`; zero counts left out). `consolidation_result.details.coverage` says the same for the judge (`judge.taken`, `judge.advanced`, `skipped_after_stalls`), and `dedup_result.supersede_apply.details.coverage` for supersede apply (`sweep.taken`, `sweep.advanced`, `sweep.pass_completed`, `sweep.passes`, `skipped_after_stalls`). A phase that failed after reading its memories still has this key: its `details` hold `coverage` alone.

### Trust boundary for memory content (prompt-injection hardening)

A memory `summary` can be **untrusted** — it may originate from external text ingested via connectors. Phases 1–4 interpolate that summary into the LLM judgment prompt, so a crafted payload in a memory body could otherwise try to steer the nightly batch's judgments (graph pollution, false merges, false promotions) in an unattended job. Two input-side defenses neutralize this (issue #919):

- **Structural isolation**: every summary is wrapped via `wrap_untrusted_content()` (`services/sleep/prompts.py`) between the explicit markers `[BEGIN UNTRUSTED MEMORY CONTENT]` … `[END UNTRUSTED MEMORY CONTENT]`. The helper also defangs any verbatim copy of those markers found *inside* the content, so a payload cannot forge the closing marker to "break out" of the wrapper. Truncation to 300 chars is applied to the raw content first.
- **Injection-resistance directive**: every phase system prompt carries `INJECTION_RESISTANCE_DIRECTIVE`, instructing the model to treat everything between the markers strictly as data and to ignore any embedded directive that tries to change its task, rules, scoring, verdicts, or output format.

**What is and isn't wrapped**: only the `summary` is untrusted and wrapped. System-controlled fields (the short label, `type`, `importance`, `scope`, `access_count`, `age_days`) are not an injection vector and are passed as-is. The non-LLM signals — tag co-occurrence and Hebbian co-activation — are deterministic and never reach an LLM prompt, so they are outside this boundary. Editing the Edge Discovery prompt requires bumping `EDGE_DISCOVERY_PROMPT_REVISION` so `sleep_reports.details` can distinguish runs. Regression coverage: `tests/services/sleep/test_sleep_prompt_injection.py`.

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
| `get_sleep_history`   | List recent sleep runs for a context with status, timing, counters and `budget_limited_phases` (#3019). `source` picks Sleep runs (`sleep`, the default), Memory Analysis spend rows (`analysis`) or both (`all`) (#2219). |
| `get_sleep_report`    | Fetch a single report with per-phase results and the full action audit log. |
| `rollback_sleep_run`  | Reverse every recorded action of a run in reverse order.                 |

### Admin REST endpoints

| Endpoint                                    | Purpose                                                   |
|---------------------------------------------|-----------------------------------------------------------|
| `GET /admin/sleep-reports`                  | List reports with filters (`status`, `source`, `context_id`, `user_id`, `workspace_id`) and pagination. `source` is `sleep`, `analysis` or `all` (default) (#2219). `workspace_id` narrows the list to one workspace (#2844). |
| `GET /admin/sleep-reports/{report_id}`      | Report detail with the full action audit log. `?include_actions=false` answers the report's own row with `actions: []` and the run's `action_count` (#2842). |
| `GET /admin/sleep-reports/{report_id}/actions` | The report's actions in pages (`limit` 1-500, default 50; `offset`), in `id` order: `{actions, total, limit, offset}` (#2842). |
| `POST /admin/sleep/run`                     | Manual run for the calling admin's own contexts (own-account: refused for a service operator). |
| `POST /admin/users/{user_id}/sleep/run`     | Manual run for a named user's context, body `{"context_id"}` (#2838). |
| `POST /admin/sleep/actions/{action_id}/undo-merge` | Undo one merge of the calling admin's own report (own-account). |
| `POST /admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge` | Undo one merge of a named report (#2838). |

A run can record many actions (one per created edge, one per merge), and a merge action's `details` hold the first characters of the merged memory's summary. A caller that needs only the report reads it with `include_actions=false`; a caller that shows the actions reads them page by page. The in-app report pages keep reading the full answer.

Every report carries `workspace_name` beside `workspace_id` (#2844): the name of a live workspace, `null` for a soft-deleted or missing one and for a report without a workspace id.

All of these require `Depends(require_admin)`. The two own-account routes refuse an operator principal (`403 SYSADMIN-004`).

### Manual run for a user (#2838)

`POST /admin/users/{user_id}/sleep/run` starts, in the background, the run the scheduled sweep would start for `(user_id, the context's workspace, context_id)`: the report's `user_id` is the target user, and the run applies the workspace's Sleep state itself. The context must be live, have a `sleep_mode` other than `skip` and hold a live memory of that user (`404 SLEEP-003` otherwise; `404 RES-001` for an unknown user; `409 SLEEP-002` while that user has a running report). The request is recorded as an `audit_logs` row (`sleep_run_trigger`) with the caller as actor and the target user, workspace, context and report ids, committed with the report. The run executes inside the process that answers the request, with that process's LLM, embedding, vector-store and Sleep settings, so a `service` process (`SYSTEM_ADMIN_MODE=service`) needs the same settings as the public processes for this route.

### Report status values

```
running  →  completed | degraded | failed | cancelled | rolled_back
```

- `completed` — every phase succeeded.
- `degraded` — the run finished, but some judge-LLM calls failed (#1183), a phase failed (`phase_failure: <phase>`, #1229) or a phase succeeded with some failed items (`reindex_partial_failure: N of M reindex operations failed`, #3036). `error_message` names the cause; `rollback_sleep_run` accepts it.
- `failed` — every judge-LLM call failed, the run stopped on a fatal error, or a rollback could not reverse every action (below).

### Rollback semantics

`rollback_sleep_run` walks the `sleep_actions` log in reverse and reverses each recorded change by type:

- **edge_created** — delete the created edge.
- **memory_merged** — restore the soft-deleted loser(s), revert winner's merged tags and content.
- **importance_updated** — restore the previous importance value (#3019: it marks the memory as re-evaluated instead of stamping `updated_at`, so the rollback does not appear as an update in `changes_since` either).
- **memory_promoted** — revert the promotion.
- **memory_archived** — restore from archive (the archives versions before #3021 made; nothing archives now).
- **apply_supersede** (#3020) — remove the link Supersede Apply wrote (or put back the edge it retyped) while it still carries the run's marker, then restore the suggestion into an empty column, marked so Sleep does not apply it again until the pair gets a new verdict. A link changed since stays (`supersede_links_kept`, not an error); a suggestion that landed since is kept.

Restored memories are re-embedded back into Qdrant. On full success the report moves to `rolled_back`; if any action fails to reverse, the report is marked `failed` and the partial state is recorded in the audit log.

Sleep does not move a memory's `version` ([#2043](https://github.com/kagura-ai/memory-cloud/issues/2043)) and writes no `memory_revisions` rows: importance re-evaluation, the tag union of a merge, promotion and every rollback above are maintenance with their own record in `sleep_actions`, so an agent's `update_memory(expected_version=N)` is never refused because a Sleep run touched the memory in between.

`rollback_session` ([#2044](https://github.com/kagura-ai/memory-cloud/issues/2044)) and Sleep stay out of each other's way: a session memory that Sleep merged away (or archived, before #3021) is skipped (`merged_by_sleep`) — undo that with `rollback_sleep_run` — and a merge winner the session created is forgotten while its loser keeps the Sleep tombstone `rollback_sleep_run` can still restore. Because Sleep does not version, a session rollback that puts back a memory's earlier tags or importance overwrites a tag union or re-evaluation Sleep made in between; the next run applies it again.

### Per-merge undo (#1209)

`rollback_sleep_run` reverses a whole run; the correction loop for a single bad merge is `POST /admin/sleep/actions/{action_id}/undo-merge` — it restores just that merge's loser (row **and** Qdrant vector) and appends an `undo_merge` action to the same report, so the merge's full history (merge → undo) reads out of one audit log. Self-scoped like the manual trigger. Error contract: `400` the action is not a dedup merge, `404` unknown/unowned action, `409` already restored (or deleted by something other than sleep), `410` the loser was purged by the Sleep retention window (`sleep_merge_retention_days`) or the platform cleanup sweep (`CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`) — reversibility is bounded by whichever window is shorter, and both bounds are named, never silent.

`POST /admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge` (#2838) is the same undo for a named report, for a system admin and a service operator: the action is found by its id and the report id, whoever the report belongs to, and an action that is not in that report answers `404`. The error contract after that is the one above. The `undo_merge` action's `undone_by` is the caller's id, or `operator` for a service operator, and an `audit_logs` row (`sleep_merge_undo`) names the caller with the report, action and memory ids.

## Admin UI

- **Sleep Reports list + detail** — `frontend/src/app/(authenticated)/admin/sleep-reports/` renders the `/admin/sleep-reports` REST endpoints as a browsable report explorer with per-action drilldown.
- **Neural Config — Sleep category** — `frontend/src/app/(authenticated)/admin/neural-config/` exposes the tuning knobs listed below without requiring an env-var redeploy.

## Tuning Knobs

All Sleep-specific settings live under the Sleep category of Neural Config (`backend/src/api/routes/neural_config.py`) and are persisted in the database. Env vars of the same name act as bootstrap defaults.

| Field                         | Purpose                                                        |
|-------------------------------|----------------------------------------------------------------|
| `sleep_llm_provider`          | `openai`, `anthropic`, `gemini` or `self_hosted`.               |
| `sleep_llm_model`             | Model identifier passed to the provider.                       |
| `sleep_max_memories_per_run`  | Upper bound on memories each phase touches per context per run (each phase has its own allowance, #3019). |
| `sleep_max_llm_calls_per_run` | Upper bound on LLM calls per context per run, shared among the phases that call a model (#3019). |
| `sleep_dedup_enabled`         | Master toggle for Phase 2.                                     |
| `sleep_merge_retention_days`  | Sleep-tombstone purge window (#1209, #1520). `0` = no additional Sleep purge; the cleanup sweep still applies (#1521). |
| `sleep_edge_discovery_enabled`| Master toggle for Phase 1. Default `false` (#3021).            |
| `sleep_importance_reeval_enabled` | Master toggle for Phase 3. Default `false` (#3021).        |
| `sleep_consolidation_judge_enabled` | LLM judge for the borderline memories of Phase 4. Default `false` (#3021); rule promotion always runs. |
| `sleep_supersede_apply_enabled` | Phase 4.5. Default `false` (#3020). |

Per-phase toggles apply on top of `sleep_mode`: a phase runs only if `sleep_mode` allows it *and* the phase's enable flag is `true`. Consolidation has no toggle of its own: its rule pass runs in every `full` run.

**Defaults and stored rows** (#3021): a `neural_config` row wins over the env var and the code default. A fresh database is seeded with `false` for the four switches above, and a database built without seed rows (`create_all`) uses the code default (`false`). A deployment migrated before v0.185.0 keeps the `true` it stored for `sleep_edge_discovery_enabled` and `sleep_importance_reeval_enabled`; to adopt the new defaults, change each through the configuration API, as a system admin:

```bash
curl -X PUT https://kagura.example.com/api/v1/admin/neural-config/sleep_edge_discovery_enabled \
  -H "Content-Type: application/json" -H "Cookie: kagura_session=..." -d '{"value": "false"}'
curl -X PUT https://kagura.example.com/api/v1/admin/neural-config/sleep_importance_reeval_enabled \
  -H "Content-Type: application/json" -H "Cookie: kagura_session=..." -d '{"value": "false"}'
```

(the cookie is a system admin's `kagura_session` session cookie in `SYSTEM_ADMIN_MODE=session`; in `service` mode send the operator credential and `X-Operator-Email` instead, see [deployment](deployment.md#system-admin-surface-issue-2767); or use the Neural Config page of the admin UI). The env variables of these keys (`SLEEP_EDGE_DISCOVERY_ENABLED`, `SLEEP_IMPORTANCE_REEVAL_ENABLED`, `SLEEP_CONSOLIDATION_JUDGE_ENABLED`, `SLEEP_SUPERSEDE_APPLY_ENABLED`) are read only for a key without a row, so on a database built by `alembic upgrade head` they change nothing. The judge switch is new: migration `f64_3021_judge_switch` stores it as `false`, so the judge is off after the upgrade on every deployment until it is switched on the same way. So does migration `f68_3020_supersede_apply_switch` for `sleep_supersede_apply_enabled` (#3020); the code reads the switch off while the row is missing, so the migration and the code update can come in either order.

With edge discovery off, a context in `edges_only` mode runs no phase but Reindex and Report.

`SLEEP_ARCHIVE_EXEMPT_TAGS` (#2584) is env-only, not a Neural Config field: the tags that keep a memory off the losing side of a merge (the name predates #3021, when consolidation archived too). A JSON array (`["status:open"]`) or a comma-separated list (`status:open,status:blocked`); unset means `status:open`, an empty value (`""` or `[]`) turns the exemption off, and a JSON value that does not parse to strings keeps the default and logs `sleep_archive_exempt_tags_invalid`. The same tags keep a memory out of the never-delivered list and count (#3022: the MCP `list` filter `never_delivered`, `GET /api/v1/contexts/{context_id}/never-delivered-count` and the context Overview card). The API service reads the variable from its own environment and the Sleep worker from its own, so set it for both; with it set on the worker only, the two use different tags.

## Related

- [Architecture](architecture.md) — Service Layer and overall system context.
- [Core Concepts](concepts.md) — Workspace, Context, and Memory primitives that Sleep operates on.
- GitHub: issues #101, #103 (original design), #164 (observability + rollback), #178 / #179 (admin UI).
