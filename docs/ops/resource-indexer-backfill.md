# Resource Indexer Backfill Runbook

When to use: you have `indexer_state` rows stuck with
`last_metrics.errors > 0` because of a bug (e.g., Issue #324's
named-vector upsert failure before the fix), and you need to re-queue
them once the upstream bug is fixed.

## Safety

Re-queueing is **idempotent**. The indexer computes each Qdrant point id as:

```
uuid5(NAMESPACE_DNS, "{resource_id}:{doc_id}:v{version}")
```

Same event → same point id → upsert overwrites in place, `points_count`
does not drift. You will not create duplicate points by re-queueing the
same `last_offset`.

## Identify affected rows

```sql
SELECT
  resource_id,
  context_id,
  last_offset,
  last_metrics->>'errors'   AS errors,
  last_metrics->>'reason'   AS reason,
  updated_at
FROM indexer_state
WHERE (last_metrics->>'errors')::int > 0
ORDER BY updated_at DESC;
```

Confirm each row's error matches the bug you just fixed (e.g.,
`"Not existing vector name error"` for #324). **Do not blindly re-queue
rows whose error is a different root cause** — they need their own fix
first.

## Re-queue procedure

Rewind `last_offset` to the earliest failing event so the indexer reprocesses
the backlog. The simplest way is to reset it to the last known-good offset, or
to `0` if you want a full re-index.

```sql
-- Option A: rewind to just before the first error
UPDATE indexer_state
SET last_offset = <known_good_offset>,
    last_metrics = jsonb_set(last_metrics, '{errors}', '0')
WHERE resource_id = '<resource_id>' AND context_id = '<context_id>';

-- Option B: full re-index from zero (safe because uuid5 keeps point ids stable)
UPDATE indexer_state
SET last_offset = 0,
    last_metrics = jsonb_set(last_metrics, '{errors}', '0')
WHERE resource_id = '<resource_id>' AND context_id = '<context_id>';
```

The next scheduled indexer run (or manual trigger) will process the pending
events. Watch the logs for `qdrant_upsert_success` and verify
`last_metrics.applied_upserts > 0`:

```sql
SELECT
  resource_id,
  last_offset,
  last_metrics->>'applied_upserts' AS applied_upserts,
  last_metrics->>'errors'          AS errors
FROM indexer_state
WHERE resource_id = '<resource_id>';
```

## Post-check

In Qdrant, confirm point count increased:

```bash
curl -s "$QDRANT_URL/collections/kagura_memories" | jq '.result.points_count'
```

If `applied_upserts > 0` but `points_count` did not change, the indexer
succeeded at the Postgres layer but the upsert did not land — re-check the
collection shape and indexer logs.
