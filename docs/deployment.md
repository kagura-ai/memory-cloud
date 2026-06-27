# Deployment Guide

## Reverse Proxy with Caddy

Kagura Memory Cloud uses [Caddy](https://caddyserver.com/) as a reverse proxy in production. Caddy provides automatic HTTPS, HTTP/2, and simple configuration.

### Example Caddyfile

```caddyfile
your-domain.example.com {
    # Health check for Caddy itself
    handle /caddy-health {
        respond "OK" 200
    }

    # Backend API
    reverse_proxy /api/* kagura-api:8080

    # Static files from backend
    reverse_proxy /static/* kagura-api:8080

    # MCP Streamable HTTP Transport
    reverse_proxy /mcp* kagura-api:8080 {
        flush_interval -1
        transport http {
            versions 1.1
        }
    }

    # OAuth2 and OpenAPI discovery endpoints
    handle /.well-known/* {
        reverse_proxy kagura-api:8080
    }

    # OpenAPI docs
    reverse_proxy /redoc kagura-api:8080
    reverse_proxy /openapi.json kagura-api:8080

    # Health check (proxied to API)
    reverse_proxy /health kagura-api:8080

    # Frontend (catch-all)
    reverse_proxy kagura-web-dev:3000
}
```

### Key Points

- **MCP endpoints** (`/mcp*`) require `flush_interval -1` for streaming support and HTTP/1.1 transport
- **`.well-known`** endpoints are needed for OAuth2 discovery (RFC 8414)
- The **frontend** acts as a catch-all for all other routes (Next.js App Router)
- Caddy automatically provisions TLS certificates via Let's Encrypt

### Docker Compose Integration

Add Caddy as a service in your `docker-compose.yml`:

```yaml
services:
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - kagura-api
      - kagura-web-dev

volumes:
  caddy_data:
  caddy_config:
```

## Frontend Environment Variables

Copy `frontend/.env.example` to `frontend/.env.local` and configure:

```bash
# Required: Backend API URL (must be accessible from the browser)
NEXT_PUBLIC_API_URL=https://api.your-domain.com

# Required: Frontend URL (for OpenGraph metadata)
NEXT_PUBLIC_APP_URL=https://your-domain.com

# Optional: Custom plan display names (default: S/M/L)
# NEXT_PUBLIC_PLAN_FREE_DISPLAY_NAME=Free
# NEXT_PUBLIC_PLAN_BASIC_DISPLAY_NAME=Standard
# NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME=Premium
```

## Tag Co-Occurrence Cold-Start Seeding (Issue #223)

Migration `b05_223_tag_cooccurrence` adds the schema; seeding fires
automatically inside the **background embedding task** (`process_pending_embedding`)
after each new memory's embedding completes — so it lags `remember()`'s synchronous
return by however long the embedding pipeline takes, and is skipped when an
embedding ultimately fails (the memory exists but no `tag_cooccurrence` edges are
created until a future re-embed succeeds). Backfilling pre-existing memories is
an opt-in operator action.

### Deploy order

1. **Deploy code.** The background embedding task (`process_pending_embedding`)
   now invokes `_create_tag_cooccurrence_seed_edges` after the existing knn
   seeding step (which runs after the Qdrant upsert succeeds). With migration
   not yet applied, the function detects that `hub_tag_cache` does not exist
   (via `SELECT to_regclass('hub_tag_cache')`) and returns silently with a
   `tag_cooccurrence_skip_pre_migration` debug log — no user-visible impact,
   no warning spam, no per-memory error rollback. The `valid_edge_type` CHECK
   constraint extension is therefore never reached in this window.

2. **Apply migration.** `make migrate` (or `alembic upgrade head`) runs
   `b05_223`, which:
   - Drops + recreates `valid_edge_type` CHECK on `neural_memory_edges`
     (allows `tag_cooccurrence` going forward).
   - Creates the GIN index `idx_memories_tags_gin` on `memories(tags)`.
     Note: this is a plain `CREATE INDEX`, **not** `CONCURRENTLY` (the repo's
     async Alembic env wraps every migration in a transaction). Lock duration
     is short because `memories.tags` is not a hot-write column. If a future
     deploy needs zero-downtime here, split the index into a dedicated
     migration that escapes the env transaction wrapping.
   - Creates the `hub_tag_cache` table (per `(workspace, context)` upsert).

3. **Wait for first Sleep Maintenance run.** Hub-tag cache populates
   automatically on the next nightly Sleep run (cron schedule from
   `SLEEP_CRON_HOUR` / `SLEEP_CRON_MINUTE`, default 02:00 UTC). Until the
   cache is populated, `remember()` proceeds with "no exclusion" — first night
   produces slightly noisier edges that subsequent nights will tighten.

4. **(Optional) Backfill existing memories.** Memories created before the
   deploy do not get tag_cooccurrence edges automatically. To populate them:

   The script lives at `/app/scripts/backfill_tag_cooccurrence_edges.py`
   inside the API container and is self-contained (no `PYTHONPATH=` override
   needed since #415 — the script's own `sys.path` setup covers both
   `/app` and `/app/src` for the codebase's mixed import style). For
   pre-#415 builds, prepend `-e PYTHONPATH=/app` to the docker exec.

   Use `docker compose exec` (against the active API color) rather than
   `docker exec -it kagura-api` because the API container is suffixed with
   the active blue/green color (`kagura-api-blue` or `kagura-api-green`):

   ```bash
   # On the VM, in /opt/kagura-memory/src/terraform/single-server:
   ACTIVE=$(cat /opt/kagura-memory/active-color)   # blue|green

   # Dry-run for a specific user (preview counts):
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod \
     exec -T api-${ACTIVE} \
     python /app/scripts/backfill_tag_cooccurrence_edges.py --user-id <uuid>

   # Execute (writes edges):
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod \
     exec -T api-${ACTIVE} \
     python /app/scripts/backfill_tag_cooccurrence_edges.py \
       --user-id <uuid> --execute --batch-size 200

   # Whole-instance backfill (long-running; consider running per-user):
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod \
     exec -T api-${ACTIVE} \
     python /app/scripts/backfill_tag_cooccurrence_edges.py --all-users --execute
   ```

   **Pre-backfill: ensure `hub_tag_cache` is populated.** The first nightly
   Sleep Maintenance run populates it automatically (cron 02:00 UTC). If
   you want to backfill BEFORE that — to get the benefit of hub-tag
   exclusion — manually populate via a one-off Python invocation:

   ```bash
   sudo docker compose -f docker-compose.prod.yml --env-file .env.prod \
     exec -T -e PYTHONPATH=/app:/app/src api-${ACTIVE} python -c "
   import sys; sys.path.insert(0, '/app/src')
   import asyncio
   from db.base import get_db
   from neural.config import NeuralMemoryConfig
   from tasks.sleep_tasks import _refresh_hub_tag_cache
   from sqlalchemy import select
   from models.memory import Memory

   async def main():
       async for db in get_db():
           cfg = await NeuralMemoryConfig.from_db(db)
           rows = (await db.execute(
               select(Memory.workspace_id, Memory.context_id).distinct().where(
                   Memory.deleted_at.is_(None),
                   Memory.workspace_id.isnot(None),
                   Memory.context_id.isnot(None),
               )
           )).all()
           for ws, ctx in rows:
               n = await _refresh_hub_tag_cache(db, workspace_id=str(ws),
                                                context_id=str(ctx),
                                                threshold=cfg.tag_cooccurrence_hub_threshold)
               await db.commit()
               print(f'  ctx={ctx} → {n} hub tags')
   asyncio.run(main())
   "
   ```

   Without this pre-step, backfill runs treat every context as "no hub tags"
   and over-edge popular tags (still bounded by the per-node degree cap, but
   noisier). The PYTHONPATH=/app:/app/src is required for the inline `python -c`
   form because it bypasses the script's own sys.path setup.

   The script is **idempotent** (safe to re-run after a crash or partial
   failure) and **resumable** via stable `id` ordering. `create_edge_if_absent`
   uses `ON CONFLICT DO NOTHING` so re-runs do not duplicate edges.

### Configuration

All knobs are DB-overridable via the admin UI's Neural Memory config page
and also settable via env (env values are the fallback when DB has no entry):

| Env var | Default | Purpose |
|---|---|---|
| `TAG_COOCCURRENCE_ENABLED` | `true` | Master switch |
| `TAG_COOCCURRENCE_MIN_SHARED` | `2` | Minimum shared tags to create an edge |
| `TAG_COOCCURRENCE_MAX_PER_REMEMBER` | `10` | Top-N matches per `remember()` call |
| `TAG_COOCCURRENCE_HUB_THRESHOLD` | `0.30` | Tag freq% above which a tag is "hub" |
| `TAG_COOCCURRENCE_MAX_DEGREE_PER_NODE` | `50` | Per-source-node degree cap |

### Disabling at runtime

To turn the feature off without redeploy: set `tag_cooccurrence_enabled=false`
in the admin Neural Memory config page (or `TAG_COOCCURRENCE_ENABLED=false`
in env, then restart the API container). Existing edges are left alone;
Sleep Maintenance prunes them naturally over time via the synthetic-seed
filter (#248 + #223 extension to `_is_synthetic_seed_edge`).


## Object Storage (S3-compatible) — Issue #994

Platform-managed file storage (`/api/v1/files/*`, Issue #485) writes to any
**S3-compatible** object store. The same `S3CompatibleStorage` client (aioboto3)
drives every backend — only the endpoint differs:

| Deployment | Backend | Endpoint |
| --- | --- | --- |
| Managed cloud (kagura) | Cloudflare R2 | `https://<account>.r2.cloudflarestorage.com` |
| Self-host (recommended) | MinIO | `http://minio:9000` |
| Self-host (AWS) | AWS S3 | leave `STORAGE_ENDPOINT_URL` empty for the region default |

### Environment variables

Canonical `STORAGE_*` names (legacy `R2_*` names are still accepted as aliases —
existing deploys keep working unchanged; a one-time deprecation line is logged
when only `R2_*` is set):

| Variable | Aliases | Default | Notes |
| --- | --- | --- | --- |
| `STORAGE_BACKEND_TYPE` | — | `r2` | `r2` \| `s3` \| `minio` \| `s3-compatible` \| `aws`. Selects the label; all use the same S3 client. |
| `STORAGE_ENDPOINT_URL` | `S3_ENDPOINT_URL`, `R2_ENDPOINT_URL` | — | **Required.** Empty ⇒ the upload path returns HTTP 502 "storage not configured". |
| `STORAGE_BUCKET` | `S3_BUCKET`, `R2_BUCKET` | — | Bucket name. |
| `STORAGE_ACCESS_KEY_ID` | `S3_ACCESS_KEY_ID`, `R2_ACCESS_KEY_ID` | — | Access key. |
| `STORAGE_SECRET_ACCESS_KEY` | `S3_SECRET_ACCESS_KEY`, `R2_SECRET_ACCESS_KEY` | — | Secret key. |
| `STORAGE_ACCOUNT_ID` | `S3_ACCOUNT_ID`, `R2_ACCOUNT_ID` | — | R2 account ID; unused by AWS S3 / MinIO (set any non-empty value). |
| `STORAGE_REGION` | `S3_REGION`, `R2_REGION` | `auto` | `auto` is correct for R2 and MinIO. Set the bucket's real region (e.g. `us-east-1`) for an `aws` backend. |
| `STORAGE_CHECKSUM_BINDING_ENABLED` | `S3_CHECKSUM_BINDING_ENABLED`, `R2_CHECKSUM_BINDING_ENABLED` | `false` | Server-side body-sha256 binding (#556). R2-specific; leave `false` on MinIO. |

### Self-host with MinIO

A `minio` service ships in `docker-compose.yml` behind the `minio` profile (it
does **not** start by default):

```bash
docker compose --profile minio up -d minio   # console at http://localhost:9001
```

> ⚠ **Security — dev defaults only.** The compose `minio` service ships with the
> well-known `minioadmin` / `minioadmin` credentials and published `9000`/`9001`
> ports for local convenience. For a real deployment, set strong unique
> `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` (or per-app bucket-scoped access
> keys), keep MinIO on the private network behind TLS, and do **not** expose
> those ports publicly. An internet-reachable MinIO with default credentials is
> an open object store (OWASP A05: Security Misconfiguration).

Then point the API at it (create the bucket once via the MinIO console or `mc`):

```bash
STORAGE_BACKEND_TYPE=minio
STORAGE_ENDPOINT_URL=http://minio:9000
STORAGE_ACCESS_KEY_ID=minioadmin
STORAGE_SECRET_ACCESS_KEY=minioadmin
STORAGE_BUCKET=kagura-files-dev
STORAGE_ACCOUNT_ID=minio
```

CI exercises the presigned PUT → GET → head round-trip against a live MinIO in
the `backend-integration` job (`backend/tests/integration/test_minio_integration.py`).

### Design note — BYO bucket per workspace (not implemented)

The seam for a future **bring-your-own-bucket** tier feature (a workspace
supplies its own S3 credentials, symmetric with the BYOK-embeddings direction)
is the `storage_backend_type` discriminator plus the per-call `S3CompatibleStorage`
construction in `storage/factory.py`. Today the factory builds one process-wide
instance from the global `STORAGE_*` settings; per-workspace BYO would move
construction behind a workspace-scoped credential lookup. Recorded here as a
seam only — no implementation in #994.

## Embedded Vector Backend: Kagura Lite (preview)

By default the backend stores vectors in **Qdrant** (the `QDRANT_URL` service).
For a **single-process, self-hosted / CLI / desktop / edge** deployment that
does not want to run a separate Qdrant server, you can switch to an embedded,
in-process **LanceDB** backend — "Kagura Lite". This is a **preview**.

### When to use which

| | Qdrant (default) | LanceDB / Kagura Lite |
|---|---|---|
| Topology | Server, multi-worker, SaaS | **Single process** (CLI / desktop / edge) |
| Extra services | Separate Qdrant container | None (a local file) |
| Concurrent writers | Yes | **No — single-writer** |
| Nightly Sleep writer | Yes | Single process only |
| Status | Stable | **Preview** |

> Keep Qdrant for any server / multi-worker / SaaS deployment. LanceDB writes
> are single-process; a multi-worker API plus the nightly Sleep maintenance
> writer would conflict.

### Enabling

```bash
# 1. Install the optional backend extra (adds lancedb + pyarrow)
pip install '.[lite]'

# 2. Configure the backend (env)
KAGURA_VECTOR_BACKEND=lance
KAGURA_LANCE_DB_PATH=./data/kagura.lance   # store location (default)
```

Japanese full-text quality is unchanged: the existing Sudachi tokenization
pipeline still owns segmentation (lemmas + readings + synonym/hiragana
augmentation); LanceDB only stores and searches the resulting vectors and
pre-tokenized FTS text. Semantic + BM25 hybrid fusion is unchanged.

### Preview limitations

- **Single-writer only.** Not for multi-process / SaaS topologies.
- These operations raise `NotImplementedError` on the lance backend: context
  copy (`copy_context_points`), cross-collection GDPR erasure
  (`delete_user_points`), and the admin BM25-drift reverse-lookup scroll.
- End-to-end LanceDB behavior is pending live validation; the backend selector
  and the SQL isolation/escaping filter are unit-tested independently of
  LanceDB.

Configuration: `KAGURA_VECTOR_BACKEND` (`qdrant` | `lance`, default `qdrant`)
and `KAGURA_LANCE_DB_PATH`. Implementation: `backend/src/db/lance_store.py`.
