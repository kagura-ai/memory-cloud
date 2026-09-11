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
| `--verify` | Every live memory id must have a point in the target collection; missing ids are listed. Target points whose memory was forgotten while the re-embed ran are deleted (`forget` only touches the collection the context routes to, so nothing else would) | untouched |
| `--switch` | One transaction: update `context_search_configs.embedding_model/embedding_dimensions` **and** re-queue (`embedding_status='pending'`) memories written since the re-embed started, so the regular 30 s sweep embeds that delta under the new routing. After the commit, points of memories forgotten since the re-embed started are dropped from the new collection, which closes the window between the last `--verify` and the flip | flipped |
| `--purge` | Delete the context's points from the source collection, in the same invocation as `--switch`. Refused while the context still routes to the source | — |
| `--purge-source MODEL` | The same deletion as a later, standalone run. Once routing has moved on, the plan can no longer name the old source by itself, so you name it. Refused while `MODEL` is what the context routes to | — |
| `--rollback-to MODEL` | Route back to `MODEL` **and** re-queue everything created or updated since the switch (the `context_search_configs.updated_at` the switch stamped; `--requeue-since` overrides it), and drop points of memories forgotten since. Refused if `MODEL`'s collection no longer exists, i.e. after a purge | flipped back |

Idempotent by construction: re-running `--reembed` overwrites points by id; `--switch` is a plain update. Each page of the re-embed is its own short read transaction, so a large context never holds a Postgres transaction open across the embedding calls.

**What the reconciliation does not cover.** Forgotten memories are found by their tombstone (`deleted_at`). A row that was hard-deleted with no tombstone while the migration ran (the 30-day cleanup of old tombstones, or a manual `DELETE`) leaves its target point behind; a later `--verify` against that context removes it, since verify reconciles every target point that has no live row.

**In-flight embedding workers.** A worker that claimed a memory just before `--switch` embeds it under the old routing. The switch re-queues that row, and the worker only records `success` while it still owns its claim (`embedding_status='processing'`), so the sweep re-embeds the row under the new routing. The log line for that case is `embedding_claim_lost`.

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

Exit code `2` means verification found memories without a target point; the routing was **not** switched for that context. Re-run `--reembed` (idempotent) and look at the listed ids if it persists.

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

The re-embed is one embedding request per batch (`--batch-size`, default 64) over every live memory of the context. Nothing is re-summarised or re-read through an LLM; only `summary` is embedded, exactly as on write.
