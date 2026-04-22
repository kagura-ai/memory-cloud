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

   ```bash
   # Dry-run for a specific user (preview counts):
   docker exec -it kagura-api \
     python scripts/backfill_tag_cooccurrence_edges.py --user-id <uuid>

   # Execute (writes edges):
   docker exec -it kagura-api \
     python scripts/backfill_tag_cooccurrence_edges.py \
       --user-id <uuid> --execute --batch-size 200

   # Whole-instance backfill (long-running; consider running per-user):
   docker exec -it kagura-api \
     python scripts/backfill_tag_cooccurrence_edges.py --all-users --execute
   ```

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

