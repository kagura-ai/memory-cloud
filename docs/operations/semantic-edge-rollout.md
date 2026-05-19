# Semantic Edge Origin Rollout (Issue #722)

Operator runbook for shipping the semantic / Hebbian edge split. The PR makes sleep-discovered edges survive the decay loop; this runbook covers the deploy steps and the one-shot backfill that restores edges previously erased by decay.

## What changed

| Layer | Before | After |
|---|---|---|
| `neural_memory_edges.origin` | (column did not exist) | `hebbian` \| `semantic` \| `declared`, default `hebbian` |
| `DecayManager` | decays + prunes all edges | only `origin='hebbian'` |
| `sleep edge_discovery` | writes fixed `weight=0.5` | writes `weight = cosine_sim`, `origin='semantic'` |
| `create_or_update_edge` upsert | unconditionally overwrites origin | sticky — only `hebbian` can be overwritten |
| `semantic_edge_reverify` cron | (did not exist) | monthly, 04:15 UTC, drops semantic edges with soft-deleted endpoints |
| Backfill | (did not exist) | `python -m scripts.backfill_semantic_edges` |

Production impact: zero user-visible change to `recall` (the graph is `explore`-only per Issue #120). The graph view and `explore()` go from near-empty to populated.

## Pre-flight

1. Verify the migration head locally:

   ```bash
   cd backend && alembic heads
   # expect: e17_722_neural_edge_origin (head)
   ```

2. Verify the running prod API is on a build that contains the merge of this PR. The deployed image SHA must include both the migration (`e17_722_neural_edge_origin`) and the new `tasks/semantic_edge_reverify.py` module.

## Deploy

1. Standard blue/green deploy via `terraform/single-server/scripts/deploy.sh`. The script handles the new color, healthchecks, and traffic flip.

2. After the new color is healthy, run the migration on the VM:

   ```bash
   gcloud compute ssh kagura-memory-vm \
     --zone=asia-northeast1-a --tunnel-through-iap --project=kagura-492509 \
     --command='docker exec kagura-api-$(cat /opt/kagura-memory/active-color) \
       alembic -c /app/alembic.ini upgrade head'
   ```

3. Verify the column landed:

   ```bash
   gcloud compute ssh kagura-memory-vm \
     --zone=asia-northeast1-a --tunnel-through-iap --project=kagura-492509 \
     --command='docker exec kagura-postgres psql -U kagura -d kagura -c "\d neural_memory_edges"' \
     | grep -E "origin|valid_edge_origin|idx_edges_origin"
   ```

   Expected lines (whitespace tolerant):

   ```
    origin       | character varying(20)  | not null | 'hebbian'::character varying
       "idx_edges_origin" btree (origin)
       "valid_edge_origin" CHECK (origin IN ('hebbian','semantic','declared'))
   ```

## Backfill

The backfill is opt-in. Skipping it means existing contexts gradually accumulate semantic edges as the next nightly sleep runs land — but historical edges already pruned by decay stay gone.

1. Dry-run with a strict threshold first to see what the impact would be on the largest context:

   ```bash
   gcloud compute ssh kagura-memory-vm \
     --zone=asia-northeast1-a --tunnel-through-iap --project=kagura-492509 \
     --command="docker exec kagura-api-\$(cat /opt/kagura-memory/active-color) \
       python -m scripts.backfill_semantic_edges \
         --context-id abfd654d-c489-47fe-a1d3-e6471041259b \
         --sim-threshold 0.85 --top-k 10 --dry-run"
   ```

   The final log line prints `Dry run: <N> edges would be inserted across 1 contexts`. If N is unreasonable (e.g. tens of thousands), lower `--top-k` or raise `--sim-threshold`.

2. Execute against the same context with the chosen threshold:

   ```bash
   gcloud compute ssh kagura-memory-vm \
     --zone=asia-northeast1-a --tunnel-through-iap --project=kagura-492509 \
     --command="docker exec kagura-api-\$(cat /opt/kagura-memory/active-color) \
       python -m scripts.backfill_semantic_edges \
         --context-id abfd654d-c489-47fe-a1d3-e6471041259b \
         --sim-threshold 0.7 --top-k 10"
   ```

3. Inspect:

   ```bash
   gcloud compute ssh kagura-memory-vm \
     --zone=asia-northeast1-a --tunnel-through-iap --project=kagura-492509 \
     --command="docker exec kagura-postgres psql -U kagura -d kagura -c \
       \"SELECT origin, COUNT(*) FROM neural_memory_edges \
         WHERE context_id = 'abfd654d-c489-47fe-a1d3-e6471041259b' GROUP BY origin;\""
   ```

   Expect a non-trivial `semantic` count (hundreds to low thousands for `kagura-dev` at 1,494 memories) and an unchanged `hebbian` count.

4. Open production backfill for all contexts (optional, can be deferred):

   ```bash
   gcloud compute ssh kagura-memory-vm \
     --zone=asia-northeast1-a --tunnel-through-iap --project=kagura-492509 \
     --command="docker exec kagura-api-\$(cat /opt/kagura-memory/active-color) \
       python -m scripts.backfill_semantic_edges \
         --sim-threshold 0.7 --top-k 10"
   ```

## Post-flight verification

* Web UI `/neural-memory` for `kagura-dev` (or any other backfilled context) shows ≥ 100 nodes / ≥ 200 edges (was 2 / 2 pre-PR).
* The next decay cycle logs `edges_pruned` ≈ 0 — the carve-out is now active. Confirm with:

   ```bash
   docker logs kagura-api-$(cat /opt/kagura-memory/active-color) --since 2h \
     | grep -E "weak_edges_pruned|bulk_decay_applied"
   ```

* `get_sleep_history(context_id='abfd654d-c489-47fe-a1d3-e6471041259b')` on the next nightly run shows `edges_created > 0` AND those edges survive in the DB past one hour:

   ```bash
   docker exec kagura-postgres psql -U kagura -d kagura -c \
     "SELECT COUNT(*) FROM neural_memory_edges \
      WHERE context_id = 'abfd654d-c489-47fe-a1d3-e6471041259b' \
        AND created_at > NOW() - INTERVAL '24 hours' \
        AND origin = 'semantic';"
   ```

* The `semantic_edge_reverify` cron will fire on the 1st of next month at 04:15 UTC. Verify the job is registered:

   ```bash
   docker logs kagura-api-$(cat /opt/kagura-memory/active-color) --since 1d \
     | grep "scheduled_semantic_edge_reverify"
   ```

## Optional config rollback

The 2026-05-19 hotfix that lowered `prune_threshold` to `0.001` and raised `sleep_edge_discovery_sample_size` to `200` is no longer load-bearing — semantic edges are now decay-exempt by design. The hotfix values can stay or be reset to defaults; neither choice changes correctness. To reset:

```bash
docker exec kagura-postgres psql -U kagura -d kagura -c "
UPDATE neural_config SET value='0.01', updated_at=NOW() WHERE key='prune_threshold';
UPDATE neural_config SET value='30', updated_at=NOW() WHERE key='sleep_edge_discovery_sample_size';
"
# API config cache TTL is 5 min — wait or restart the api container to pick up.
```

## Rollback

If something goes wrong after deploy:

1. **Code rollback**: deploy the previous color back. Both the column-additive migration AND the new code defaulting `origin='hebbian'` are forward- and backward-compatible at the wire level.

2. **Migration rollback** (only needed if the column itself is the problem — almost never):

   ```bash
   docker exec kagura-api-$(cat /opt/kagura-memory/active-color) \
     alembic -c /app/alembic.ini downgrade -1
   ```

   The downgrade is idempotent (`DROP CONSTRAINT IF EXISTS`).

3. **Backfill rollback**: bulk-delete the inserted rows by origin. They cannot have been inserted before this deploy, so the time bound is a safe filter:

   ```bash
   docker exec kagura-postgres psql -U kagura -d kagura -c \
     "DELETE FROM neural_memory_edges \
      WHERE origin = 'semantic' \
        AND created_at > '<deploy_timestamp>';"
   ```

Backfill rows are additive — leaving them in place is safe even when rolling back code.
