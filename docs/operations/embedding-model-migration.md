# Migrating a Context to Another Embedding Model (Issue #1525)

> **Related**: [Architecture § Embedding Models](../architecture.md) · [Deployment](../deployment.md) · `EMBEDDING_MODEL_REGISTRY` in `backend/src/config/constants.py`

A context's embedding model is immutable through the API (#146): two models cannot share a collection, and flipping the model on a context with data would make every existing memory unsearchable. This runbook is the supported way to change it anyway — for example to move a deployment off a paid embedding API onto a self-hosted or OpenAI-compatible model — **without a window in which the context has no vectors**.

> **Placeholders**: `<CONTEXT_ID>`, `<API_CONTAINER>` and the upstream model id below are stand-ins for your deployment's values.

## How it works

Vectors are derived data. Every memory's `summary` is in Postgres, and the normal write path (`process_pending_embedding`) rebuilds a Qdrant point from the row. The migration runs that against a second collection while the context keeps serving from its current one:

| Step | What happens | Routing |
|---|---|---|
| `--plan` | Resolve source (config row or legacy fallback) and target (registry + `EMBEDDING_MODEL_ALLOWLIST`), derive both collection names, count memories | untouched |
| `--reembed` | Create the target collection if needed; embed every live memory with the target model; upsert with the **same point id, payload and BM25 sparse vector** the write path produces | untouched |
| `--verify` | Every live memory must have a point in the target collection; missing ids are listed. Target points whose memory was forgotten while the re-embed ran are deleted (`forget` only touches the collection the context routes to, so nothing else would) | untouched |
| `--switch` | One transaction: update `context_search_configs.embedding_model/embedding_dimensions` **and** re-queue (`embedding_status='pending'`) memories written or embedded since the re-embed started, so the regular 30 s sweep embeds that delta under the new routing. A memory a Sleep `undo_merge` or `rollback_sleep_run` restored counts as embedded when it was restored (the restore re-embeds it under the old routing and stamps `embedding_attempted_at`), so one restored after `--verify` is re-queued too: the re-embed skipped it while it was deleted. After the commit, points of memories forgotten since the re-embed started are dropped from the new collection, which closes the window between the last `--verify` and the flip | flipped |
| `--purge` | Delete the context's points from the source collection, in the same invocation as `--switch`. Refused while the context still routes to the source | — |
| `--purge-source MODEL` | The same deletion as a later, standalone run. Once routing has moved on, the plan can no longer name the old source by itself, so you name it. Refused while `MODEL` is what the context routes to | — |
| `--rollback-to MODEL` | Route back to `MODEL` **and** re-queue everything created, updated or embedded since the switch (the switch time the switch recorded, `context_search_configs.embedding_switched_at`; a context switched before v0.116.0, or never switched by this command, has none, and the command uses the search config's last change and prints a warning — pass `--requeue-since <switch time>` if the settings were edited after the switch; `--requeue-since` always overrides), and drop points of memories forgotten since. The boundary is printed before the confirmation prompt. Refused if `MODEL`'s collection no longer exists, i.e. after a purge | flipped back |

Idempotent by construction: re-running `--reembed` overwrites points by id; `--switch` is a plain update. Each page of the re-embed is its own short read transaction, so a large context never holds a Postgres transaction open across the embedding calls.

The switch waits up to 30 seconds for point writers before changing routing, and never waits for a memory row: its re-queue locks the rows it changes `FOR NO KEY UPDATE NOWAIT` (#2156), so it cannot deadlock with a session rollback or a Sleep restore holding them. If writers remain active, or another transaction holds memories the switch must re-queue, it leaves routing unchanged, exits `1` with a `NOT switched` (or `NOT rolled back`) line, and `--all` goes on to the next context. The line prints the retry: after `--run`, `--verify --switch --requeue-since <the re-embed start>` (no full re-embed; a `--switch` given `--requeue-since` gets the same with its time); after a bare `--switch`, `--switch` again; after a refused rollback, `--rollback-to` again (with `--requeue-since` only if you passed it). On a busy context the retry may be refused again for an ordinary write in progress; run it once that write finishes. After a successful route commit, pruning waits for writers for at most another 30 seconds; if that wait expires, the route remains switched, the command reports deferred pruning, and the orphan-vector sweep can remove the stale summary and chunk points.

Review the sweep plan with `python -m src.cli.sweep_orphan_vectors`, then run `python -m src.cli.sweep_orphan_vectors --apply --yes --grace-hours 0` to apply it immediately. The apply pass rechecks each candidate while holding the same point-writer lock. The default one-hour grace period remains preferable for routine runs.

**Resource-ingested memories** (documents a resource or a connector ingested, #1896). Their `summary` is only the label `[resource] doc vN`, and their point is not stored under the memory id but under the document's own point id (`memories.summary_embedding_id`, derived from resource / document / version). The migration treats them the way the indexer and the embedding sweep do:

- `--reembed` rebuilds each one from the stored document: the document is projected through the resource's latest schema, its text is embedded with the target model (one embedding request per document; these rows are not batched), and the point is written under the document's point id with its facets and sortable fields. A schema published since the document was ingested applies, as it would to a re-index.
- `--verify` looks for that point id, keeps it, and still removes target points no live memory names (of any context: a document two contexts ingested is one point in a collection they share). `--switch` and `--rollback-to` drop a forgotten document's point under the same id; `--purge` / `--purge-source` delete by context, so they cover resource points as well.
- A document that **cannot be rebuilt** — the resource has no schema left, or the memory's content was edited into something that is not a JSON document — is skipped, not given a vector of its label; a point an earlier migration to the same model left for it in the target collection is removed, unless another live memory names it. `--reembed` and `--verify` list those memories on an `UNREBUILDABLE` line (the log event is `context_embedding_resource_unrebuildable`, with the reason). They do not count as missing, so they do not hold back the switch and the exit code stays `0`; after the switch they have no vector in the new collection, so semantic recall does not find them. Ingest the document again as a newer version (the same version is refused as a duplicate) to make it searchable.

A context migrated before v0.96.0 has label-only vectors for its resource-ingested memories, stored under the memory ids. Ingesting the documents again as newer versions repairs them.

**Content chunk vectors** (#2015). A context with `content_chunks_enabled` on keeps its chunk points through the migration:

- `--plan` prints about how many chunk points the re-embed writes (`content chunks on: the re-embed also writes ~N chunk point(s)`), computed from the content lengths.
- `--reembed` rebuilds each page's chunk points in the target right after its summary points, with the target model and the same deterministic ids, deleting first (a memory whose content got shorter loses its old chunks). The flag is read again before each page's chunks are deleted and before they are written, so turning it off mid-run stops the chunk writes. A chunk failure stops the run like a summary failure does; re-run `--reembed`. A flag turned **on** after `--plan` is picked up from the next page of that re-embed: `--verify` then reports the memories of the pages before it, and `--run` rebuilds their chunks before it switches (see the next item).
- `--verify` compares each live memory's chunk count in the target with what its current content splits into (counts only: an edit that keeps the count is not seen, and an edit after the re-embed started is re-queued by the switch anyway) and lists those that differ on a `CHUNKS_MISSING` line. On its own `--verify` only reports them (exit code `0`; the line says `Not switched yet`): re-run `--reembed`, or `--run`, before switching. When a switch follows in the same invocation (`--run`, or `--verify --switch`), the listed memories' chunks are rebuilt in the target first (one confirmation covers the rebuild and the switch; the flag is read again, the same delete-then-write as the re-embed), their chunks are counted again, and only then does the switch go ahead. Memories the switch re-queues anyway (changed or embedded since the re-embed started) are left to the sweep and do not hold it back. If a rebuilt memory's chunk count still differs, the context is **not** switched and the exit code is `2`; a failed chunk write stops that context with exit code `1` and the run goes on to the next one. Retry just those memories with `--verify --switch --requeue-since <the re-embed start the line prints>` (no full re-embed), and look for `content_chunks_*` errors in the log if the same ids stay listed. Chunk points of memories that are no longer live are deleted; with the flag off, every chunk point of the context in the target is.
- After a switch the migration cannot rebuild chunks any more: `--reembed --to <target>` is refused because the context already uses that model, and `--verify` needs the same plan. A context switched with chunks missing (a bare `--switch` without `--verify`, or the flag turned on later) still finds those memories through their summary points. To give them chunk points, either route back with `--rollback-to <source>` (before any purge) and `--run` again, or re-queue the memories for the embedding sweep (`embedding_status='pending'`), which writes their chunks under the new routing.
- `--switch` (and `--rollback-to`) also delete the chunk points of memories forgotten since the re-embed started, in the collection routed to. Re-queued memories rewrite their chunks through the regular sweep. `--purge` / `--purge-source` delete by context, so chunk points go with the rest.
- Turning the flag off deletes the context's chunk points from **every** collection, so the source and the target agree about it. Only a failure in the collection the context routes to keeps the flag on; a failure in another collection is logged (`content_chunks_purge_failed`) and retried after the commit, and `--verify` with the flag off removes what is left in the target.

**What the reconciliation does not cover.** Forgotten memories are found by their tombstone (`deleted_at`). A row that was hard-deleted with no tombstone while the migration ran (the 30-day cleanup of old tombstones, or a manual `DELETE`) leaves its target point behind; a later `--verify` against that context removes it, since verify reconciles every target point that has no live row.

**In-flight embedding workers.** A worker that claimed a memory just before `--switch` embeds it under the old routing. The switch re-queues that row, and the worker only records `success` while it still owns its claim (`embedding_status='processing'` and the `embedding_attempted_at` stamp its claim holds; a live worker renews that stamp), so the sweep re-embeds the row under the new routing. The log line for that case is `embedding_claim_lost`.

**During the migration** a context is searched against whichever collection it routes to, so cross-context recall between a migrated and a not-yet-migrated context fails the same way it does today for contexts on different models. Migrate a deployment's contexts in one pass rather than over days.

## Prerequisites

1. The target model is in `EMBEDDING_MODEL_REGISTRY` and, if `EMBEDDING_MODEL_ALLOWLIST` is set, listed there.
2. The provider that model routes to is reachable from the API container. For `self_hosted` models that means `SELF_HOSTED_BASE_URL` (and `SELF_HOSTED_API_KEY` if the backend wants one). If the backend serves the model under a different id than the registry name, set `SELF_HOSTED_MODEL_ALIASES`, e.g.

   ```
   SELF_HOSTED_MODEL_ALIASES=qwen3-embedding:4b=Qwen/Qwen3-Embedding-4B
   ```

   Only the wire `model` changes; the registry name still names the collection, the cache key and the allowlist entry.
3. Do **not** set `KAGURA_RECREATE_COLLECTIONS`. The migration creates the target collection through the same `ensure_kagura_memories_collection` path as startup and never deletes a collection.
4. Qdrant backend (verification retrieves points by id, which the LanceDB preview store does not support).

## Running it

Inside the API container (same environment as the API):

```sh
docker exec -it <API_CONTAINER> python -m src.cli.migrate_context_embedding \
  --to qwen3-embedding:4b --context <CONTEXT_ID>            # plan only (read-only)

docker exec -it <API_CONTAINER> python -m src.cli.migrate_context_embedding \
  --to qwen3-embedding:4b --context <CONTEXT_ID> --run      # reembed + verify + switch (asks before switching)
```

Whole deployment, non-interactive:

```sh
docker exec -it <API_CONTAINER> python -m src.cli.migrate_context_embedding \
  --to qwen3-embedding:4b --all --run --yes
```

Exit code `2` means verification found memories without a target point, or, before a switch, memories whose content chunks were still incomplete after the rebuild (`CHUNKS_MISSING ... NOT switched`); the routing was **not** switched for that context. Re-run `--reembed` (idempotent) and look at the listed ids if it persists. Resource-ingested memories on the `UNREBUILDABLE` line are not part of that count (see above).

After every context is switched:

1. Watch `sweep_pending_embeddings` in the API logs drain the re-queued delta (usually seconds).
2. Point new contexts at the new model: set `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` and, if you want to stop offering the old model, `EMBEDDING_MODEL_ALLOWLIST`. Setting the allowlist **before** every context is migrated is the wrong order — context creation with the old model would start failing while existing contexts still use it.
3. Only once you are satisfied, drop the source points:

   ```sh
   docker exec -it <API_CONTAINER> python -m src.cli.migrate_context_embedding \
     --purge-source text-embedding-3-small --context <CONTEXT_ID>          # or --all --yes
   ```

   Until then `--rollback-to <old-model>` restores the previous routing and re-queues what was written since the switch. After a purge there is nothing to roll back to; the command refuses.

## Cost and duration

The re-embed is one embedding request per batch (`--batch-size`, default 64) over every live memory of the context. Nothing is re-summarised or re-read through an LLM; only `summary` is embedded, exactly as on write. Resource-ingested memories are the exception: each is one embedding request of its document text.

With `content_chunks_enabled` on, each memory of 1,500 characters or more adds up to 16 chunk embedding inputs (one per 1,200-character chunk), sent in requests of at most 64 chunks — up to 17 embeddings per long memory instead of one. `--plan` prints the estimate for the context.
