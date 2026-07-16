# Ops plan: `memory_access_events` retention and partitioning (RFC-0002 F7)

- **Status**: Operational baseline since v0.49.0; governs the first `memory_access_events`
  retention escalation
- **Issue**: [#1264](https://github.com/kagura-ai/memory-cloud/issues/1264) — gating item F7 of
  RFC-0002 (Agent Memory & Context Control Plane; RFC text maintained locally, lands in
  `docs/rfc/0002-agent-memory-context-control-plane.md` when published)
- **Consumers**: operators of production deployments (measurement cadence + escalation
  execution), implementers of the escalation migration, P0-5 maintainers (F3, #1260 — the
  model-docstring trigger note, the no-TRUNCATE trigger, and the erasure carve-out shape are
  inputs to the schema sign-off)
- **Depends on**: MemoryAccessEvent schema sign-off (F3, #1260) for the canonical table and
  append-only trigger; the account-erasure lane
  (`backend/src/services/account_erasure_service.py`, runbook `docs/ops/erasure-runbook.md`)

`memory_access_events` is RFC-0002's append-only audit table for the eight memory operations
(`recall`, `reference`, `remember`, `update`, `forget`, `load_pinned`, `bootstrap`,
`feedback`). v1 deliberately ships with **unlimited retention and no partitioning**; what it
ships instead is an explicit escalation trigger. The RFC defers the partitioning-vs-TTL choice
because it needs production data on agent-heavy workloads. This plan therefore does three
things now, so that nothing is improvised at escalation time: it **fixes the escalation
trigger** (with a soft threshold that makes it actionable), **fixes the measurement plan**
operators run until the trigger, and **fixes the decision structure** — criteria, a
provisional default (monthly range partitioning), and the evidence that would flip it.

The v0.49.0 writer currently emits bootstrap, load-pinned, feedback, recall, reference, and
remember. The table already accepts all eight operation values; `update`/`forget` emission
and deny persistence remain tracked by #1286 and do not change this retention policy.

## Scope and non-goals

**In scope (normative):**

- The escalation trigger, its clock-start definition, and the soft threshold.
- The measurement plan (queries, cadence, what gets recorded).
- The partitioning-vs-TTL decision: criteria, provisional default, flip evidence, and an
  execution sketch for each branch.
- The retention ↔ erasure interplay rules (append-only trigger carve-out).
- The volume-basis re-size gate for the P1 unbound-traffic audit extension.

**Non-goals:**

- **No deletion window is adopted here.** The v1 "keep everything" posture stands. Escalation
  changes the *storage shape* of the table; actually deleting audit history is a separate
  per-deployment governance decision that requires its own privacy review.
- Nothing ships with this document — no migration, no scheduler job. Implementation happens in
  the escalation issue this plan tells operators to file.
- Retention for the other event lanes (`usage_stats`, `audit_logs`, `llm_call_log`,
  `retrieval_feedback`, `secret_access_log`) is out of scope; they have their own postures.
- Whether the P1 unbound-traffic extension ships at all is F3's CSO/privacy review, not this
  plan. This plan only fixes the sizing gate that enabling it must pass.

## Restated RFC-0002 decisions this plan builds on

This document is self-contained; the decisions it depends on are restated here rather than
cited by RFC line number.

| Decision | Restatement | In-repo precedent (verified) |
|---|---|---|
| D25 — separate table, unlimited v1 retention, explicit trigger | `memory_access_events` is quota-invisible by construction (never counted by `usage_stats` consumers). v1 has no partitioning and no TTL; the escalation trigger is **100M rows or 12 months of P0 traffic, whichever first**, documented in the model docstring. Monthly range partitioning on `occurred_at` is the pre-designated additive escalation response; the final partitioning-vs-TTL call needs production data. No sampling, ever — an audit lane that samples is not an audit lane. | docstring-trigger pattern: `backend/src/models/llm_call_log.py` ("Out of scope" section — 100M-row escalation note); quota-invisibility rationale: `ContextReadAttribution` in `backend/src/models/auth.py` |
| D34 — audited population | P0 writes rows **only for requests carrying verified agent identity** (credential-bound `agent_id` or verified baggage claim, incl. shadow-mode `would_deny`). Volume scales with *agent adoption*, not total traffic. Auditing unbound traffic is a P1 extension behind a deployment setting, default off — and it **must re-open this plan's sizing before it can flip on**. | — |
| D22 — append-only trigger with a narrow carve-out | A `BEFORE UPDATE OR DELETE` trigger blocks all DELETEs and blocks any UPDATE touching a column other than `user_id`, `session_id`, `run_id`, `event_metadata`. The carve-out exists solely so GDPR/APPI erasure can pseudonymize and scrub. No HMAC chain in P0. | trigger tier: `secret_access_log_append_only` + `secret_access_log_no_truncate` in `backend/alembic/versions/e50_1128_secret_store.py` |
| D23 — erasure = pseudonymize-and-keep | Account erasure never deletes audit rows; it rewrites `user_id` (salted hash via `audit_pseudo_salt`), pseudonymizes `session_id`/`run_id`, and scrubs `event_metadata`. | `_pseudonymize_field` / `_pseudonymize_audit_logs` in `backend/src/services/account_erasure_service.py`; `audit_pseudo_salt` in `backend/src/config/settings.py` |
| D21 — no FKs; the trail survives entity deletion | Rows outlive agents/contexts/keys, so nothing cascades; the table joins the erasure story explicitly instead. | survives-deletion posture: `SecretAccessLog` in `backend/src/models/secrets.py` |
| D24 — writer is fail-open in P0 | The event writer runs on an independent session and swallows write failures with a structured warning (validation errors still raise). Consequence for this plan: brief maintenance locks degrade audit coverage, not user-facing requests. | closest posture precedent: `backend/src/services/llm_call_log_writer.py` — it swallows write failures with a structured warning when callers pass `fail_on_error=False` (validation errors always raise) and uses an injected session; the P0-5 writer strengthens this to always-fail-open on an independent session per D24 |

## Escalation trigger (normative)

- **Hard trigger**: `memory_access_events` reaches **100,000,000 rows**, or **12 months** have
  elapsed since the deployment's first P0 audit row (verified-agent-identity traffic) —
  **whichever comes first**. The clock starts per deployment at the first row the P0-5 writer
  persists, not at RFC merge or code deploy.
- **Soft threshold** (start executing, don't start thinking): any of
  - estimated rows ≥ **50,000,000**, or
  - **month 9** since the first P0 row, or
  - the measurement plan's projection puts the 100M crossing **less than 6 months out**.

  At the soft threshold the operator files the escalation issue and runs the decision
  procedure below, so the chosen mechanism is merged and rehearsed *before* the hard trigger.
- **What the hard trigger obligates**: the escalation decision must be *executed* (not merely
  filed) — the table must be under the chosen mechanism before it grows materially past the
  trigger.
- **Docstring requirement** (input to F3/#1260): the ORM model docstring must state this
  trigger and link to this document, following the `backend/src/models/llm_call_log.py`
  "Out of scope" precedent, so the trigger is discoverable next to the schema.

## Measurement plan

Run monthly (first business day), from the month the P0-5 writer goes live. All queries are
read-only and assume the indexes the F3 sign-off (#1260) defines for the P0-5 migration
(`idx_mae_occurred`, `idx_mae_workspace_occurred`, `idx_mae_agent_occurred` — they ship
with the table, not before). `occurred_at` is naive UTC by repo convention; sessions run
with `timezone=UTC` (engine-enforced), so `now()` comparisons are correct.

```sql
-- 1. Estimated total rows (cheap; refreshed by autovacuum/ANALYZE).
--    Schema-qualified regclass avoids hitting a same-named relation in
--    another schema on deployments with a non-default search_path.
SELECT reltuples::bigint AS estimated_rows
FROM pg_class WHERE oid = 'public.memory_access_events'::regclass;

-- 2. On-disk footprint (heap + indexes + TOAST)
SELECT pg_size_pretty(pg_total_relation_size('public.memory_access_events')) AS total_size;

-- 3. Trailing 30-day insert rate and naive projection to the 100M line.
--    GREATEST(0, ...) clamps the remaining-row count so the projection
--    reads 0 (crossed) instead of going negative past the threshold.
WITH rate AS (
  SELECT count(*) / 30.0 AS rows_per_day
  FROM memory_access_events
  WHERE occurred_at >= now() - interval '30 days'
)
SELECT rows_per_day::bigint AS rows_per_day,
       CASE WHEN rows_per_day > 0 THEN
         (GREATEST(0, 100000000 - (SELECT reltuples FROM pg_class
                                   WHERE oid = 'public.memory_access_events'::regclass))
          / rows_per_day)::int
       END AS days_to_100m
FROM rate;

-- 4. Volume drivers: operation/outcome mix over the trailing 30 days
SELECT operation, outcome, count(*) AS n
FROM memory_access_events
WHERE occurred_at >= now() - interval '30 days'
GROUP BY operation, outcome
ORDER BY n DESC;

-- 5. Concentration: how many distinct agents produce the volume
SELECT count(DISTINCT agent_id) AS active_agents
FROM memory_access_events
WHERE occurred_at >= now() - interval '30 days';
```

Record per check, in the deployment's ops log (never in this public doc): `estimated_rows`,
`total_size`, `rows_per_day`, `days_to_100m`, the top operation/outcome buckets, and
`active_agents`. Two derived signals feed the decision below:

- **Growth shape**: is `rows_per_day` rising with agent adoption (volume-bound trajectory) or
  flat (time-bound trajectory)?
- **Volume drivers**: a mix dominated by `recall`/`load_pinned` success rows is organic; a mix
  dominated by `denied`/`would_deny` rows means the volume problem is a misconfigured or
  probing agent and should be fixed at the source before any storage escalation.

## Decision: monthly range partitioning vs TTL sweep

The two mechanisms answer the escalation differently. Partitioning changes the table's
*shape* so any future bulk removal is an O(1) DDL operation and the table stays manageable
while still keeping everything. A TTL sweep is a *deletion policy* executed as row DELETEs —
it only makes sense together with an adopted retention window, and it must punch a hole in
the D22 append-only trigger to run at all.

### Provisional default (normative): monthly range partitioning on `occurred_at`

The default response at the trigger is to convert `memory_access_events` to a
declaratively range-partitioned table, one partition per calendar month. Rationale, in
order of weight:

1. **Append-only compatibility.** The D22 trigger blocks row DELETEs by design.
   `DETACH`/`DROP PARTITION` is DDL — it does not fire row-level triggers — so partition-level
   removal is the only bulk-removal shape that leaves the append-only guarantee fully intact
   on live data. A TTL sweep, by contrast, requires amending the trigger itself.
2. **Cost at the trigger's scale.** Dropping a month is O(1); deleting a month out of a
   100M-row table is millions of dead tuples, vacuum pressure, WAL volume, and index churn —
   recurring forever.
3. **Pre-designation.** RFC-0002 D25 already names monthly range partitioning on
   `occurred_at` as the additive escalation response; this plan confirms it as the default
   rather than re-opening it.
4. **Audit-lane honesty.** An audit trail should shed history in uniform, non-selective time
   slices, not row-by-row. Whole-month granularity is the shape a compliance reviewer can
   reason about.

### Evidence that flips the default to a TTL sweep

The default is flipped — in the escalation issue, with the measurement record attached — if
production data at the soft threshold shows **all** of:

- the trigger is being hit by **time** (12 months) rather than volume, with estimated rows
  well under the 100M line (order of ≤ 20M) **and** a flat `rows_per_day` trajectory — the
  small-table case, where the partition-conversion swap and the monthly maintenance machinery
  outweigh a nightly sweep; **and**
- a deletion window has actually been adopted for the deployment (a TTL sweep with nothing to
  delete is pure risk), **and**
- no requirement has emerged for retention granularity that whole-month drops cannot express
  (if a *per-workspace contractual* window emerges, neither branch as sketched suffices and
  the decision returns here for a redesign).

Absent all three, the default stands. "Partitioning looks like more work this quarter" is
explicitly not flip evidence.

### Execution sketch — partitioning branch (default)

To be finalized and rehearsed in the escalation issue; the fixed points are:

1. **Native PostgreSQL declarative partitioning, no pg_partman.** Production runs stock
   `postgres` (18+, digest-pinned since #1302 — exact pin in `docker-compose.yml`,
   `terraform/single-server/docker-compose.prod.yml`);
   pg_partman would mean a custom image, while partition maintenance fits the existing
   APScheduler pattern (`backend/src/tasks/scheduler.py`; job-registration precedent
   `schedule_file_tasks` in `backend/src/tasks/file_tasks.py`, which already runs a nightly
   03:15 UTC GC).
2. **Conversion is a table swap** (PostgreSQL cannot partition a table in place): create the
   partitioned parent under a temporary name, then prepare the legacy table — both
   preparations outside the swap transaction — so the swap itself does metadata work only:
   - Pre-add a `CHECK (occurred_at < '<boundary>')` constraint (`NOT VALID` then `VALIDATE`,
     the house pattern for hot tables) so `ATTACH PARTITION` skips its validation scan.
   - Pre-create on the legacy table, via `CREATE INDEX CONCURRENTLY`, a **unique index on
     `(id, occurred_at)`** plus an index matching every partitioned index the parent carries.
     `ATTACH PARTITION` auto-builds any parent index still missing a match on the partition —
     and the unique index backing the new `(id, occurred_at)` PK (step 3) has no match,
     because the legacy PK is on `id` alone. At the ~100M-row escalation scale that build
     would otherwise run *inside* the swap transaction under `ACCESS EXCLUSIVE`, turning
     seconds into hours. The three shipped indexes already exist on the legacy table and
     match automatically if the parent's partitioned indexes mirror their definitions
     verbatim; the unique index is the one that must be built new.

   Then, in one short transaction: rename the legacy table to a historical-partition name,
   rename the parent to `memory_access_events`, attach the legacy table as the historical
   partition (`FROM (MINVALUE) TO ('<boundary>')`), and create current-month, next-month, and
   `DEFAULT` partitions. With the constraint and indexes pre-staged, `ATTACH` only does
   metadata matching, so the swap takes a brief `ACCESS EXCLUSIVE` lock; because the writer
   is fail-open (D24), the worst case is a seconds-long audit-coverage gap, not user-facing
   failures — schedule it in a low-traffic window anyway and note it in the ops log.
3. **Primary-key wrinkle.** A partitioned table's unique constraints must include the
   partition key, so the parent's PK becomes `(id, occurred_at)`; `id` keeps its existing
   sequence, and `id`-keyset pagination keeps working off the PK index. The three shipped
   indexes are recreated as partitioned indexes (the `agent_id` one stays partial).
4. **Triggers.** The two guards attach at different levels, because PostgreSQL clones only
   *row-level* triggers from a partitioned parent to its partitions. The D22 append-only
   trigger (`BEFORE UPDATE OR DELETE … FOR EACH ROW`) is recreated once on the parent —
   row-level triggers on a PG15+ partitioned parent cascade to all partitions, including
   future ones. The no-TRUNCATE trigger (`BEFORE TRUNCATE … FOR EACH STATEMENT`, the
   `e50_1128_secret_store.py` shape) does **not** cascade: a parent-only copy guards
   `TRUNCATE memory_access_events` but a direct `TRUNCATE <partition>` would bypass it,
   violating interplay rule 2 below. It must therefore exist on every partition: the
   historical partition keeps its F3-shipped trigger through the rename-and-attach; the
   initial current-month, next-month, and `DEFAULT` partitions get it at creation inside the
   escalation migration; every future partition gets it from the maintenance job (step 5).
5. **Maintenance job.** A monthly APScheduler job creates the partition for month N+2 ahead
   of time — including its per-partition no-TRUNCATE trigger (step 4) — and alerts
   (structured log) if the `DEFAULT` partition has received any rows — `DEFAULT` is a safety
   net for clock skew, not a working partition.
6. **Partitioning deletes nothing.** Dropping/detaching partitions begins only if and when a
   deletion window is adopted (governance + privacy review, out of scope here). Until then the
   table keeps everything; each month is simply a separately manageable slice.

### Execution sketch — TTL-sweep branch (only if flipped)

1. Prerequisite: an adopted, privacy-reviewed deletion window.
2. **Trigger amendment, not trigger removal**: a migration replaces the append-only trigger
   body so DELETE is allowed **only** when `OLD.occurred_at` is older than the retention
   window, with the window **hard-coded in the trigger body** — never a GUC, session variable,
   or application-supplied value, so no compromised session or app bug can widen it. All other
   DELETEs and all UPDATEs outside the D22 carve-out columns remain blocked.
3. Nightly APScheduler job (off-peak, the `file_tasks.py` 03:xx UTC precedent), deleting in
   bounded batches with a commit per batch and structured-log counts.
4. Vacuum/bloat monitoring becomes part of the monthly measurement check — the sweep turns an
   append-only table into a high-churn one, which is precisely why this branch is not the
   default.

## Retention interplay with the erasure lane (normative)

These rules hold on both branches, from P0-5 day one:

1. **Erasure is pseudonymize-and-keep and rides the carve-out.** The only row UPDATEs the
   trigger permits touch `user_id`, `session_id`, `run_id`, `event_metadata` — and the only
   legitimate writer of those UPDATEs is the account-erasure lane
   (`backend/src/services/account_erasure_service.py`; operator procedure in
   `docs/ops/erasure-runbook.md`). Retention, cleanup, or backfill machinery MUST NOT
   piggyback on the carve-out.
2. **Bulk removal happens at partition granularity or through the amended trigger — nothing
   else.** Permitted shapes: `DETACH`/`DROP PARTITION` (default branch) or the
   expiry-predicate DELETE (TTL branch). Forbidden always: row UPDATEs outside the carve-out
   columns, `TRUNCATE` (the schema carries a no-TRUNCATE trigger per the
   `e50_1128_secret_store.py` precedent — an F3 requirement), and
   `ALTER TABLE … DISABLE TRIGGER` as an operational shortcut.
3. **Erasure and retention commute.** A partition drop that happens to remove an erased
   subject's rows is fine (strictly less data); an erasure pass on the partitioned shape
   sweeps all partitions transparently, because the carve-out UPDATE never touches the
   partition key (`occurred_at`), so no row movement occurs. There is no ordering requirement
   between the two lanes.
4. **Per-subject deletion requests are never served by row deletion.** The audit trail's
   answer to GDPR/APPI is pseudonymization (D23), so no erasure path ever needs a DELETE —
   which is what makes rule 2's closed list sufficient.
5. **Escalation acceptance criterion**: the erasure integration coverage for
   `memory_access_events` (the F3 pseudonymize/scrub pass) must be re-run against the
   post-swap partitioned shape before the escalation migration merges.

## P1 unbound-traffic extension: volume re-size gate (normative)

The D34 population makes v1 volume scale with **agent adoption** (≥ 1 row per `recall` plus
per `load_pinned` per agent-bound turn). The P1 unbound-traffic audit extension
(all-principal logging including deny events; setting-gated, default off; own CSO/privacy
review under F3) changes the volume class to **total traffic** — every legacy client's recall
gains a hot-path INSERT.

Per D25/D34, the extension **MUST NOT be enabled** in any deployment until this plan's sizing
is re-opened. Concretely, before flipping the setting:

1. Measure the would-be audited all-principal rate (proxy: `usage_stats` rows/day for the
   recall/load_pinned/bootstrap-shaped operations, versus current `memory_access_events`
   rows/day) and recompute `days_to_100m` under the extension.
2. If the recomputed projection crosses the hard trigger within 12 months, execute the
   partitioning default **before** enabling the extension — do not enable and race the
   trigger.
3. Record the re-size as a dated addendum section appended to this document (same file, same
   sign-off discipline). Enabling the extension without that addendum violates the RFC's
   sizing MUST.

## Migration reality notes

- The RFC sketch's revision-id guidance ("chain from head `e60_1228_read_attributions` as
  `e61_*`/`e62_*`") is stale. F1/P0-2 landed as `e63`/`e64`/`e65`, P0-5 landed as
  `e66_1278_memory_access_events`, and the agent-key invariant followed as `e67`. A later
  escalation migration chains from the then-current head — revision ids ≤ 32 chars, linear
  chain, symmetric downgrade.
- Both integration gates must pass for every migration in this lane:
  `backend/tests/integration/test_alembic_migrations.py` (round-trip incl. `downgrade -1`) and
  `backend/tests/integration/test_create_all_vs_alembic_drift.py`. The partitioned shape will
  need explicit handling in the drift gate — the ORM model can declare
  `postgresql_partition_by` so `create_all` matches the parent, but the swap-produced
  historical partition and the month children exist only on the alembic side — call this out
  in the escalation issue rather than discovering it in CI.
- The table keeps its public name `memory_access_events` through the swap, so the
  data-boundary classification and erasure wiring added by #1278
  (`OPERATIONAL_TABLES` in `backend/src/models/data_boundary.py`, enforced by
  `backend/tests/test_derived_layer_boundary.py`) stay valid through the escalation without
  changes.

## Sign-off checklist (maps to #1264)

- [x] **Escalation trigger fixed**: 100M rows or 12 months of P0 (verified-agent-identity)
      traffic, whichever first — with a per-deployment clock-start definition, a soft
      threshold (50M rows / month 9 / <6-month projection) that makes it actionable, and a
      monthly measurement plan feeding it
- [x] **Monthly range partitioning vs TTL sweep decided as a structured decision**:
      provisional default = monthly range partitioning on `occurred_at` (append-only
      compatibility, O(1) drops, D25 pre-designation), with the exact production evidence
      that flips it to a TTL sweep and an execution sketch for both branches; the volume
      basis is re-sized behind a normative gate before the P1 unbound-traffic audit
      extension may be enabled
- [x] **Retention interplay with the erasure pseudonymize/scrub lane fixed**: the append-only
      trigger carve-out (`user_id`, `session_id`, `run_id`, `event_metadata`) is reserved for
      the erasure lane; bulk removal happens only via partition detach/drop or the
      narrowly-amended trigger expiry predicate — never row UPDATEs outside the carve-out,
      never TRUNCATE, never trigger disablement
