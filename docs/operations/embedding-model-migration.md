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
| `--verify` | Every live memory id must have a point in the target collection; missing ids are listed | untouched |
| `--switch` | One transaction: update `context_search_configs.embedding_model/embedding_dimensions` **and** re-queue (`embedding_status='pending'`) memories written since the re-embed started, so the regular 30 s sweep embeds that delta under the new routing | flipped |
| `--purge` | Delete the context's points from the source collection. Refused while the context still routes to the source | — |
| `--rollback-to MODEL` | The same switch with the previous values. Works as long as the source points were not purged | flipped back |

Idempotent by construction: re-running `--reembed` overwrites points by id; `--switch` is a plain update.

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
3. Only once you are satisfied, `--purge` per context (or `--all --purge --yes`) to drop the source points. Until then `--rollback-to <old-model>` restores the previous routing instantly.

## Cost and duration

The re-embed is one embedding request per batch (`--batch-size`, default 64) over every live memory of the context. Nothing is re-summarised or re-read through an LLM; only `summary` is embedded, exactly as on write.
