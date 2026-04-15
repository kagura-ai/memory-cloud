# Resource Indexer Backfill Runbook

When to use: you have `indexer_state` rows stuck with `metrics.errors > 0`
because of a bug (e.g., Issue #324's named-vector upsert failure before the
fix), and you need to re-queue them once the upstream bug is fixed.

## Safety

Re-queueing is **idempotent**. The indexer computes each Qdrant point id as:

```
uuid5(NAMESPACE_DNS, "{resource_id}:{doc_id}:v{version}")
```

Same event → same point id → upsert overwrites in place. `points_count` does
**not** grow when re-processing events whose points already exist. You will
not create duplicate points by re-queueing the same `last_offset`.

## Identify affected rows

The metrics JSONB column is named `metrics` (not `last_metrics`).

```sql
SELECT
  resource_id,
  context_id,
  last_offset,
  metrics->>'errors'   AS errors,
  metrics->>'reason'   AS reason,
  updated_at
FROM indexer_state
WHERE (metrics->>'errors')::int > 0
ORDER BY updated_at DESC;
```

Confirm each row's error matches the bug you just fixed (e.g.,
`"Not existing vector name error"` for #324). **Do not blindly re-queue
rows whose error is a different root cause** — they need their own fix
first.

## Re-queue procedure

Rewind `last_offset` to the earliest failing event so the indexer reprocesses
the backlog. The simplest way is to reset it to the last known-good offset, or
to `0` if you want a full re-index. Use `coalesce(metrics, '{}'::jsonb)` in
case the column is NULL (server_default is `{}`, but be defensive).

```sql
-- Option A: rewind to just before the first error
UPDATE indexer_state
SET last_offset = <known_good_offset>,
    metrics = jsonb_set(coalesce(metrics, '{}'::jsonb), '{errors}', '0'::jsonb)
WHERE resource_id = '<resource_id>' AND context_id = '<context_id>';

-- Option B: full re-index from zero (safe because uuid5 keeps point ids stable)
UPDATE indexer_state
SET last_offset = 0,
    metrics = jsonb_set(coalesce(metrics, '{}'::jsonb), '{errors}', '0'::jsonb)
WHERE resource_id = '<resource_id>' AND context_id = '<context_id>';
```

The next scheduled indexer run (or manual trigger) will process the pending
events. Watch the logs for `qdrant_upsert_success` and verify the metrics:

```sql
SELECT
  resource_id,
  last_offset,
  metrics->>'applied_upserts' AS applied_upserts,
  metrics->>'errors'          AS errors
FROM indexer_state
WHERE resource_id = '<resource_id>';
```

`errors` should drop to `0`. `applied_upserts` should be non-zero on the
first run that reprocesses the backlog. On subsequent runs with no new
events, it will be `0` and `skipped=true, reason=no_pending_events` —
that's the steady state, not a failure.

## Post-check

Because upserts are idempotent, `points_count` **is not a reliable
success signal** — re-processing an existing event overwrites the same
point id without changing the count. Verify success by one of:

1. **Sample a known point.** Compute the point id for a specific
   `(resource_id, doc_id, version)` that was in the failing batch:

   ```python
   from uuid import uuid5, NAMESPACE_DNS
   print(uuid5(NAMESPACE_DNS, f"{resource_id}:{doc_id}:v{version}"))
   ```

   Then retrieve it from Qdrant:

   ```bash
   curl -s "$QDRANT_URL/collections/kagura_memories/points/<point-id>" \
     | jq '.result | {id, has_vector: (.vector != null), payload_keys: (.payload | keys)}'
   ```

   A successful backfill returns the point with `has_vector=true` and the
   expected payload keys (`resource_id`, `doc_id`, `version`, `content`).

2. **Compare `points_count` against a pre-backfill baseline.** If this is
   the first time any events for a `(resource_id, context_id)` pair have
   successfully indexed, `points_count` WILL grow. Capture the baseline
   before re-queueing:

   ```bash
   curl -s "$QDRANT_URL/collections/kagura_memories" | jq '.result.points_count'
   ```

   Compare to the count after the indexer run.

If `applied_upserts > 0` but neither check above succeeds, the indexer
reported success at the Postgres layer but the upsert did not land in
Qdrant — re-check the collection shape (`GET /collections/kagura_memories`
should report `dense` as a named vector) and the indexer logs.
