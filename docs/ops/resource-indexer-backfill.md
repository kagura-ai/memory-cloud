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

## Re-run the events a run skipped (#2254)

The indexer does not retry a failed event. It logs `indexer_event_failed` and
carries on with the batch, and as soon as a later event succeeds `last_offset`
has moved past the failure, so nothing will pick it up again. A failed event
at the end of a batch stays above `last_offset` and runs again with the next
run, once one is queued. `indexer-status` reports the count as
`state.metrics.failed_events`; `metrics.failed_events` on the row lists the
events (`event_id`, `doc_id`, `error_class`), the oldest 20.

The list grows until a batch finishes with no failures. That batch empties it
even though the skipped events were not re-applied, so rewind soon after the
count shows up. A run with nothing pending, a held-back run and a run that
fails outright keep the list. For anything older, the `indexer_event_failed`
log lines are the record.

```sql
SELECT resource_id, context_id, last_offset, job_status, metrics->'failed_events'
FROM indexer_state
WHERE jsonb_array_length(coalesce(metrics->'failed_events', '[]'::jsonb)) > 0;
```

Fix the cause first (`error_class`, and the log line's `error`), then rewind
to just before the lowest `event_id`: the indexer reads `id > last_offset`, so
`last_offset = min(event_id)` would skip that event again. Do this while
`job_status` is not `running`, or the run in progress overwrites the rewind.
`LEAST` keeps a row whose offset never passed the failure where it is.

A rewind replays every event after that point, in order, for every document in
the resource, not only the failed ones. The point ids stay stable (see
[Safety](#safety)), but the rows do not:

- Replaying an older version of a document retires its newer version's row, and
  replaying the newer version writes a new row with a new id. Edits made on the
  row since (tags, importance, details) are lost, and so is anything keyed to
  its id.
- A document that was forgotten comes back as a visible row, unless a later
  `delete` event in the replay removes it again.
- Until the replay reaches the latest version or the `delete`, the older state
  is what recall serves.

So rewind as little as you can, check the events between the target and
`last_offset` first, and weigh this against sending the failed documents again
through ingest, which touches only those documents: an upsert at a new version
(the same version is refused as a duplicate), a delete as it was.

```sql
SELECT id, op, doc_id, version, created_at
FROM resource_events
WHERE resource_pk = (
    SELECT resource_pk FROM indexer_state
    WHERE resource_id = '<resource_id>' AND context_id = '<context_id>'
  )
  AND id >= <min_event_id> AND id <= <last_offset>
ORDER BY id;
```

```sql
UPDATE indexer_state
SET last_offset = LEAST(
      last_offset,
      (SELECT min((e->>'event_id')::bigint)
       FROM jsonb_array_elements(metrics->'failed_events') AS e) - 1
    ),
    job_status = 'queued',
    next_run_at = now() AT TIME ZONE 'utc'
WHERE resource_id = '<resource_id>' AND context_id = '<context_id>'
  AND job_status <> 'running';
```

One run handles 100 events and then parks the row until the next ingest event.
After a longer rewind, queue the row again (`job_status`, `next_run_at`) until
`last_offset` is back where it was. A batch with no failures empties
`metrics.failed_events`; an event that fails again is listed again.

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

## Metadata backfill for points indexed before v0.122.0 (#2254)

When to use: once, after upgrading a deployment that indexed resource
documents before v0.122.0. Since v0.122.0 the indexer writes the owning
memory's `type`, `tags` and `context_summary` into each resource point, so
recall's payload filters see them. Points indexed earlier carry none of the
three and gain them only when Sleep reindexes them, an embedding migration
re-embeds them, or someone edits the memory. Until then
`filters.type="resource_data"` does not return them (tags are empty on
unedited rows, so tag filters are not affected).

`resource_point_metadata_backfill` walks every live, embedded memory that owns
a resource point (its `summary_embedding_id` is the `uuid5` of its
`resource_id:doc_id:vN`) and sets the three keys on that point from the row.
It is set-payload only: no vector is deleted or re-embedded, no other payload
key changes, and there is no migration. Running it again writes the same
values again.

Run it where the API runs (same environment), e.g. inside the API container,
from `backend/`:

```bash
# 1. Plan (the default, read-only): how many memories and contexts it covers
python -m src.cli.resource_point_metadata_backfill

# 2. Optional canary: one workspace first
python -m src.cli.resource_point_metadata_backfill --workspace <workspace-id> --apply --yes

# 3. Apply to the whole deployment
python -m src.cli.resource_point_metadata_backfill --apply --yes
```

`--apply` without `--yes` asks before writing; `--yes` without `--apply` is a
usage error (exit 2). The apply exits 0 when every point was written or
skipped, 1 on an error or when any memory is listed `failed`. `--log-level DEBUG` prints one vector-store line per
point.

The apply report prints `updated <n> resource point(s)` and
`points written: <n>` (the points actually written), then, when they occur:

- `skipped <n> point(s) shared with another live memory` — the point is named
  by a second live memory as well; it is left as it is, as a metadata edit
  would leave it.
- `skipped <n> memory(ies) deleted or changed during the run` — each row is
  read again right before its point is written; one deleted, erased or
  pointed at another point since the plan is left alone.
- `failed <memory-id>` — one line per memory whose point could not be
  written; the walk carries on with the rest, and the command exits 1. The
  log line `resource_point_metadata_backfill_row_failed` holds the error. A
  transient vector-store error clears on a second run. A point that no longer
  exists ("No point with id ...") has no vector at all; queue the memory for
  the embedding sweep, which rebuilds a resource point from its row (with
  `type`, `tags` and `context_summary`) under the same point id:

  ```sql
  UPDATE memories
  SET embedding_status = 'pending', embedding_error = NULL, embedding_retry_count = 0
  WHERE id = '<memory-id>' AND deleted_at IS NULL;
  ```

Post-check: retrieve a point the plan covered and confirm the keys are there.

```bash
curl -s "$QDRANT_URL/collections/<collection>/points/<point-id>" \
  | jq '.result.payload | {type, tags, context_summary}'
```

`type` is the memory's type (`resource_data` for documents the indexer
wrote) and `tags` is a list (empty for unedited memories). A recall in the
resource's context with `filters.type="resource_data"` now returns its
documents.
