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
- Caddy writes **no access log** unless the site has a `log` directive. If you add
  one, bound the container's log file ([Container log rotation](#container-log-rotation))
  and keep invite tokens out of it (see
  [Closed-beta invite links](#closed-beta-invite-links-issue-1581)) — the
  single-server template's `Caddyfile.tpl` does both

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
    # Bound the container log — see "Container log rotation" below.
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
    depends_on:
      - kagura-api
      - kagura-web-dev

volumes:
  caddy_data:
  caddy_config:
```

### Container log rotation

Docker's default `json-file` log driver keeps one **unbounded** file per
container. A proxy that logs every request — and MCP clients poll — can grow
that file to several GB within weeks and fill the disk; the usual first symptom
is an image build that fails for lack of space.

Every service in the single-server compose files
(`terraform/single-server/docker-compose.{prod,app,data,ollama}.yml`) therefore
declares the following, so the stack is bounded on any host — not only on one
whose Docker daemon was configured for it:

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"   # rotate at 50 MB ...
    max-file: "3"     # ... keep 3 files: at most ~150 MB per container
```

For Caddy that is still enough access log for incident triage; ship the logs
elsewhere if you need longer retention.

**Applying it to a running stack.** `logging` options are fixed when a container
is *created* — `docker compose restart` does not apply them. After updating to a
release that carries the block:

| Service | How it picks the option up |
|---|---|
| `api-blue` / `api-green` | `deploy.sh` recreates the color it deploys to on every run — nothing to do (the other color follows with the next deploy). |
| `web` | Recreated by `deploy.sh --web`, or once by hand (below). |
| `caddy` | **Never recreated by `deploy.sh`** (it only restarts Caddy) — recreate it once by hand (below). |
| `postgres` / `qdrant` / `redis` (and `ollama`) | Recreating them **restarts the database**, so do it in a maintenance window. Their logs are small; it can wait for the next planned one. |

```bash
cd /opt/kagura-memory/src/terraform/single-server

# caddy + web, once. :80/:443 drop for a few seconds while caddy is replaced.
# Do it AFTER the release's deploy.sh run (or `./scripts/deploy.sh
# --generate-caddyfile`), so the new container starts on the re-rendered
# ./Caddyfile. On a registry-mode host (KAGURA_IMAGE_SOURCE=registry) add
# --no-build.
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  up -d --no-deps --force-recreate caddy web

# Verify
docker inspect -f '{{json .HostConfig.LogConfig}}' kagura-caddy
# {"Type":"json-file","Config":{"max-file":"3","max-size":"50m"}}

# Data tier — maintenance window only (volumes are kept, the services restart):
# docker compose -f docker-compose.prod.yml --env-file .env.prod \
#   up -d --no-deps --force-recreate postgres qdrant redis
```

On a split-host layout use `docker-compose.app.yml` on the app VM and
`docker-compose.data.yml` (plus the `data-expose` overlay) on the data VM.

Recreating a container also **discards its old log file**, so the recreate is
what frees the space taken by an already oversized log — and what removes
access-log lines written before the
[invite-token scrub](#closed-beta-invite-links-issue-1581) was in place.

A host-level default remains a good belt-and-braces measure, because it also
covers containers that are not part of this compose project.
`terraform/single-server/startup.sh` already writes it on the VM it provisions;
on any other host add it to `/etc/docker/daemon.json` and restart Docker (like
the compose option, it only applies to containers created afterwards):

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
```

## Frontend Environment Variables

Copy `frontend/.env.example` to `frontend/.env.local` and configure:

```bash
# Required: Backend API URL (must be accessible from the browser)
NEXT_PUBLIC_API_URL=https://api.your-domain.com

# Required: Frontend URL (for OpenGraph metadata)
NEXT_PUBLIC_APP_URL=https://your-domain.com

# Optional: Custom plan display names (default: S/M/L/XL)
# NEXT_PUBLIC_PLAN_FREE_DISPLAY_NAME=Free
# NEXT_PUBLIC_PLAN_BASIC_DISPLAY_NAME=Standard
# NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME=Premium
# NEXT_PUBLIC_PLAN_PROMAX_DISPLAY_NAME=Premium Max
```

## Closed-beta invite links (Issue #1581)

For a deployment that runs with the **signup gate closed** (Admin → Signup
gate: enabled, mode `manual`), invite links let existing users bring people in
without an admin adding each identity by hand. A signed-in user mints a
one-time URL (`{FRONTEND_URL}/join/{token}`); whoever opens it and signs in
with Google or GitHub within 7 days passes the gate once, and is recorded on
the signup allowlist at that moment (source `beta_invite`, added by = the
inviter) where admins can see and prune it as usual.

| Variable | Default | Effect |
|----------|---------|--------|
| `ENABLE_BETA_INVITES` | `false` | Master switch **and kill switch**. When `false` every `/api/v1/beta-invites*` route answers 404, `features.beta_invites` is `false` (the web UI shows no entry points), an `invite=` parameter on OAuth login is ignored, and **redemption at the OAuth callback is disabled too** — links already handed out stop working. Allowlist rows written by earlier redemptions are not touched; remove them on the admin signup-gate page. |
| `BETA_INVITE_QUOTA_PER_USER` | `4` | Links a non-admin user may hold at once. Active (unused, unexpired) and redeemed links count; expired and revoked ones free their slot. System admins (`role=admin`) are uncapped. `0` lets only system admins mint. |

The 7-day lifetime is fixed in code. Notes for operators:

- A link is a credential. Only its SHA-256 hash is stored (database and the
  short-lived OAuth-state key in Redis); the URL is shown once, at creation. A
  lost link cannot be recovered — the inviter **reissues** it (#1595), which
  revokes the old link and mints a replacement in one step.
- An inviter can attach a **label** to a link (#1595) — free text of up to 100
  characters, typically the recipient's name or address. It is inviter-private:
  stored on the `beta_invites` row, returned only to that inviter, never written
  to a log line or an audit row, and deleted with the inviter's account. On a
  redeemed link the inviter also sees the admitted account's **current e-mail**,
  for as long as that account exists — it is read from the live `users` row, so
  erasing the account (or pruning its allowlist entry) removes it from the
  inviter's view as well.
- The token travels in URLs (`/join/{token}`, `/api/v1/beta-invites/{token}/preview`,
  and `…/login?invite={token}`). The API scrubs it from its own output — structured
  logs, the uvicorn access log and the `usage_stats` table all record the literal
  `{token}` instead. **A reverse proxy in front of the API or the frontend is
  outside that reach** and has to scrub its own logs. The single-server
  Terraform template already does (#1591), in both places Caddy writes a request
  down: the site's `log` block in `Caddyfile.tpl` (the access log, stdout) and
  `log default` in its global options (Caddy's default logger, stderr — it
  carries the `http.log.error` line written about every request whose upstream
  failed, e.g. a `502` while the frontend is being restarted). Each wraps the
  JSON encoder in a `filter` that writes `REDACTED` into the token slot of those
  three URL shapes — in the request URI and, for the access log, in the
  `Location` / `Refresh` response headers, which repeat the path when the
  frontend answers `/join/{token}/` with its trailing-slash redirect — and drops
  the `Referer`, `Next-Router-State-Tree`, `Next-Url` and `Cookie` request
  headers from the log. Ordinary requests keep their full URI. The
  response-header rewrite needs Caddy 2.6.2 or newer (an older 2.6 image skips
  it silently); `docker compose pull caddy` refreshes the floating
  `caddy:2-alpine` tag. **If you run a different proxy, replicate that before
  enabling the feature** (or restrict who can read its logs). The copies to
  cover: the request URI; the `Referer` header a browser on
  the `/join/{token}` page would attach to the preview call, the
  `/auth/{provider}/login` navigation and every asset the page loads — the
  frontend serves `/join/*` with `Referrer-Policy: no-referrer` (response header
  plus a `<meta name="referrer">` backstop, #1588), so current browsers send no
  `Referer` from that page, but keep the proxy-side filter anyway if your proxy
  overwrites response headers or you cannot vouch for the clients; the
  router-state headers (`Next-Router-State-Tree`, `Next-Url`) the Next.js client
  sends on data and prefetch requests, which carry the current route including
  the token segment; redirect response headers; and the proxy's **error log** —
  an upstream failure (`502`) records the same request URI and headers there,
  usually through a different logger than the access log. Filtering only the
  URI, or only the access log, leaves the other copies in place. Lines written
  before the scrub was deployed are not rewritten — they age out with
  [log rotation](#container-log-rotation), or go at once when the proxy container
  is recreated.
- The invite is only consumed when the gate would otherwise have blocked the
  sign-in. Existing users, the first user, identities already on the allowlist,
  and any deployment with the gate disabled (where `ALLOW_REGISTRATION` decides)
  never use one up.
- Invitees are ordinary users and get their own quota, so invitations chain.
  `ENABLE_BETA_INVITES=false` stops the chain immediately.
- Minting, redemption and revocation are written to the audit log as
  `beta_invite.created` / `beta_invite.redeemed` / `beta_invite.revoked`, naming
  the invite by id only. A reissue writes one `revoked` and one `created` row
  whose `user_metadata` cross-reference the two invites (`reissued_from` /
  `reissued_to`, ids only — never the label).

### Invites and device or MCP sign-in (Issue #1655)

An invite only counts when it rides the OAuth login that `/join/{token}`
builds. Two other sign-in paths start at `/login` instead: the device flow
(`/device` sends a signed-out visitor to `/login?return_to=/device?user_code=…`)
and an MCP client's OAuth sign-in (`/api/v1/oauth/authorize` sends it to
`/login?return_to=<the authorize URL>`). Two ways carry an invite through them:

- **`/join/{token}?return_to=<path>`** — the invite link accepts an optional
  `return_to`. After sign-up the new user lands there instead of the
  dashboard. A CLI can print one link that signs up with the invite and resumes
  the device login, for example
  `https://<host>/join/<token>?return_to=%2Fdevice%3Fuser_code%3D<code>`.
  `return_to` is only a destination, checked the same way as on `/login`: a
  single-`/` path or a same-origin `http(s)` URL, with no backslash or control
  character, checked again by the API before the post-login redirect. A value
  that fails the check is dropped silently and the link still works as a plain
  invite (dashboard). An already signed-in visitor gets a link to the
  `return_to` path; the "back to login" link on the expired, invalid, disabled
  and error screens keeps it too.
- **"I have an invite link" on `/login`** — shown when `features.beta_invites`
  is on. The person pastes the link (or the bare token); the page sends them to
  `/join/{token}` with the page's own `return_to` beside it, so an MCP sign-in
  continues after sign-up. A value that is not a `/join/{token}` link or a
  well-formed token (`[A-Za-z0-9_-]`, 20–128 characters) shows an inline error
  and goes nowhere. The pasted token is never logged or stored by the page.

`/join/{token}` asks for the same terms-of-service acceptance as `/login`: the
provider buttons stay disabled until the box is ticked. With `TERMS_VERSION`
set the acceptance is also recorded on the server — see
[Terms-of-service acceptance](#terms-of-service-acceptance-issue-1665).

The MCP path resumes only when the API and the frontend share an origin. On a
split-origin deployment `/login` already drops the API-origin `return_to`, with
or without an invite. The device flow is not affected: its `return_to` is a
frontend path.

## Terms-of-service acceptance (Issue #1665)

`/login`, `/join/{token}` and the workspace invitation page ask for
terms-of-service acceptance with a checkbox. By default that is all they do: a
browser-side check. Setting `TERMS_VERSION` makes the server enforce and record
it.

| Variable | Default | Effect |
|----------|---------|--------|
| `TERMS_VERSION` | *(empty)* | The current terms version, any label you choose (`2026-09`, `v3`, …; 1–64 characters from `A-Z a-z 0-9 . _ -`, anything else fails startup). **Empty disables the feature**: nothing is enforced, nothing is recorded, nobody is asked to re-accept, and every sign-in request is exactly what it was before. |

With a version set:

- `GET /api/v1/system/info` reports it as `terms_version` (`null` when empty).
  The sign-in pages send it back as `accepted_terms` once the box is ticked —
  on the Google / GitHub login URL, where it is bound to the OAuth `state` in
  Redis beside `return_to` and the invite (5-minute TTL, read and deleted once
  by the callback after the CSRF check), and in the password-login body.
- **New accounts need it.** A Google or GitHub sign-in whose identity has no
  account yet is refused unless it carries the current version: no account is
  created, the signup gate never runs (so a beta invite is not spent), and the
  browser lands on `/login?error=terms_required` with its `return_to` kept.
  This also covers a direct request to `/api/v1/auth/{provider}/login` without
  the parameter. Password login never creates accounts; accounts created with
  the admin CLI are asked on first sign-in like any existing user.
- **Existing users are never locked out.** A sign-in with a missing or older
  version succeeds. The web UI then sees `terms_acceptance_required: true` on
  `GET /api/v1/auth/me` and shows a blocking "updated terms" dialog; accepting
  calls `POST /api/v1/me/terms-acceptance` (the user can sign out instead).
- Each acceptance is one row in `terms_acceptances` (user, version, source
  `login` / `join` / `password` / `reaccept`, timestamp) and one audit row
  `terms.accepted` carrying the version only. A sign-in with the version the
  user already accepted writes nothing, so rows appear only when the accepted
  version changes. The history is deleted with the account.

**Changing `TERMS_VERSION`** (a new version of the terms) asks every existing
user to accept again on their next page load — nobody is signed out, and
sessions, API keys and MCP clients keep working; only the web UI is blocked
until they accept. Sign-ups in flight with the old version (a login page opened
before the change) are refused and see the `terms_required` banner; the page
they return to already carries the new version. **Clearing it** switches
enforcement off again; the recorded history stays in `terms_acceptances`.

Adding an account from the account switcher follows the same rule: an identity
that has no account yet is refused until it signs up from `/login`.

## Hosted-mode UI gates (Issue #1571)

The web UI reads `GET /api/v1/system/info` → `features.*` at runtime, so a
deployment hides a surface with a backend env var, not a frontend rebuild.
The three toggles that shape a hosted deployment:

| Variable | Default | When `false` |
|----------|---------|--------------|
| `ENABLE_COST_DISPLAY` | `true` | Hides money from workspace users. The workspace cost dashboard disappears (`/workspace/cost` nav entry hidden, the page shows a "not enabled" notice, `GET /workspaces/{id}/cost-aggregation` answers 404). The Memory Analysis "Run cost" KPI, history "Cost" column and pre-flight "Estimated cost" are not rendered, and the analysis payloads carry no cost: REST `cost_estimated_cents` / `cost_actual_cents` / `estimated_cost_cents` are `null` (shape kept), the MCP `get_analysis` / `list_analyses` / `get_active_analysis` / `analyze_context` dry-run dicts omit the keys. `GET /admin/cost-aggregation` and the `/admin/cost` page are **unaffected** — operators still see what the platform spends. |
| `ENABLE_BYOK` (#1167) | `true` | Closes external-key provisioning (create / update answer 404) and the key-status probe; also hides the workspace cost dashboard. Both `ENABLE_BYOK` and `ENABLE_COST_DISPLAY` must be `true` for that dashboard to show. Keys stored earlier stay listable, toggleable and deletable by the workspace owner — including `OPENAI_API_KEY`, which is never "Required" with BYOK off (#1613). The External Keys nav entry stays for the owner of a workspace that has such keys and is hidden otherwise (#1616). |
| `ENABLE_PLAN_PAGE` (#1145) | `false` | Keeps the owner Plan page + nav entry hidden (no billing to hand off to on a self-hosted deployment). |

A flat-price hosted deployment typically runs `ENABLE_COST_DISPLAY=false`
(the platform-billed USD is the operator's own cost) with `ENABLE_PLAN_PAGE=true`.
OSS / self-hosted keeps the defaults and sees today's UI.

**Resources / Connectors navigation** needs no env var. Since #1551 creating
them is XL-only (`resources` / `connectors` in the plan-tier matrix, see
[Plan Tiers](#plan-tiers)); the sidebar shows an entry when the plan includes
the feature **or** the workspace already owns at least one such object
(objects created before a downgrade keep working). For a plan without the
feature the sidebar asks the list endpoint once per session (owner for
resources, admin+ for connectors — the roles the entries already require);
the entry stays hidden while either answer is pending, so a lower tier never
sees a flash-then-hide. The pages themselves stay reachable by URL and carry
the upsell state for new objects.

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
> well-known `minioadmin` / `minioadmin` credentials and publishes `9000`/`9001`
> on `127.0.0.1` for local convenience (`COMPOSE_BIND_HOST` widens that; see
> [Reaching the data stores](getting-started.md#reaching-the-data-stores)). For
> a real deployment, set strong unique `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`
> (or per-app bucket-scoped access keys), keep MinIO on the private network
> behind TLS, and do **not** expose those ports publicly. An internet-reachable
> MinIO with default credentials is an open object store (OWASP A05: Security
> Misconfiguration).

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
  copy (`copy_context_points`) and the admin BM25-drift reverse-lookup scroll.
  Cross-collection GDPR erasure (`delete_user_points`) IS supported as of
  #1336 (per-table SQL delete across `kagura_memories*`).
- End-to-end LanceDB behavior is pending live validation; the backend selector
  and the SQL isolation/escaping filter are unit-tested independently of
  LanceDB.

Configuration: `KAGURA_VECTOR_BACKEND` (`qdrant` | `lance`, default `qdrant`)
and `KAGURA_LANCE_DB_PATH`. Implementation: `backend/src/db/lance_store.py`.

## Reranking — Issue #1572

Recall is hybrid (semantic + BM25); a **cross-encoder reranker** can re-score
the candidate window before the top *k* is returned. Reranking is resolved per
context from its search config (`use_rerank`, `reranker_provider`,
`reranker_model` — the context Settings tab, `PUT /contexts/{id}/search-config`,
or the `update_search_config` MCP tool) and gated three ways at recall time:

1. **Plan** — the `reranking` feature: Basic (M) and up by default; Free (S)
   never reranks. Override per tier with `PLAN_<KEY>_FEATURES` (see Plan Tiers).
2. **Deployment** — `ENABLE_RERANKING=false` is the kill switch: no context
   reranks, whatever its config says.
3. **Caller** — `recall(use_rerank=...)`: **omit it to follow the context's
   config**; `false` forces reranking off; `true` still requires the context to
   allow it.

Providers: `voyage` and `cohere` are **BYOK** (a workspace-scoped external API
key resolved at recall time — there is no platform-key tier); `self_hosted` is
**keyless** and talks to an endpoint you run.

### Environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `ENABLE_RERANKING` | `true` | Deployment kill switch. `false` ⇒ no context reranks; `/system/info` reports `features.reranking=false` and the web UI hides the reranker card from the context search settings. |
| `DEFAULT_RERANKER_PROVIDER` | `voyage` | `voyage` \| `cohere` \| `self_hosted`. Written to **new** context search configs. |
| `DEFAULT_USE_RERANK` | `false` | `use_rerank` written to new context search configs. `true` with `self_hosted` requires `RERANK_BASE_URL` or `SELF_HOSTED_BASE_URL`, else the API refuses to start. |
| `DEFAULT_RERANKER_MODEL` | — | Model written to new configs. Empty ⇒ the provider's default (`rerank-2`, `rerank-multilingual-v3.0`, or for `self_hosted` `RERANK_MODEL` when `RERANK_BASE_URL` is set, else `SELF_HOSTED_RERANK_MODEL`). For voyage/cohere it must be a model the UI offers. |
| `RERANK_BASE_URL` | — | OpenAI/Jina-style `/v1/rerank` endpoint (vLLM `--runner pooling`, TEI, Infinity). When set, `self_hosted` makes **one batched** `POST {RERANK_BASE_URL}/v1/rerank` per recall. |
| `RERANK_MODEL` | `qwen3-reranker-0.6b` | Served model name on that endpoint (vLLM `--served-model-name`). |
| `RERANK_API_KEY` | — | Bearer token when the `/v1/rerank` endpoint is behind auth. |
| `SELF_HOSTED_RERANK_MODEL` | `dengcao/Qwen3-Reranker-8B:Q5_K_M` | Prompt-scoring fallback on `SELF_HOSTED_BASE_URL` (`/v1/completions`, one call per document), used only when `RERANK_BASE_URL` is unset. The default is an Ollama-registry id — a pure-vLLM stack must set a model it actually serves. |

The `DEFAULT_*` values apply to contexts created from now on; **existing rows
are never rewritten by a migration** (#1207 decision) — see the recipe below.
Unset, they reproduce the previous behaviour (`false` / `voyage` / `rerank-2`).

`GET /api/v1/system/info` (public) exposes `features.reranking`
(`ENABLE_RERANKING` and, for a `self_hosted` default, an endpoint is configured)
and `search_defaults: {use_rerank, reranker_provider, reranker_model}` — names
only, never URLs or keys.

### Self-hosted recipe (keyless reranking as standard equipment)

Serve a reranker behind `/v1/rerank` and make it the default for new contexts:

```bash
# e.g. vLLM: vllm serve Qwen/Qwen3-Reranker-0.6B --runner pooling \
#            --served-model-name qwen3-reranker-0.6b --port 8002
RERANK_BASE_URL=http://reranker:8002
RERANK_MODEL=qwen3-reranker-0.6b
DEFAULT_RERANKER_PROVIDER=self_hosted
DEFAULT_USE_RERANK=true
```

A freshly created context on a plan with `reranking` now reranks with no user
action and no external key; a Free workspace does not
(`reranking_disabled_by_plan_tier` in the log). Then convert the contexts that
already exist (one-shot, idempotent):

```bash
# inside the API container / venv, from backend/
python -m src.cli.apply_rerank_defaults --all                # plan: convert/skip per context, writes nothing
python -m src.cli.apply_rerank_defaults --all --apply --yes  # write; --workspace <uuid> narrows the scope
```

Only rows still carrying the **code default** (`use_rerank=false`,
`reranker_provider=voyage`, `reranker_model` `rerank-2` or `rerank-2-lite`) are
converted; any other value is treated as an explicit choice and left alone. An
owner who explicitly picked the old default is indistinguishable and is
converted too. Re-running after `--apply` changes 0 rows.

### Fail-open behaviour

A reranker outage never fails a recall. When the provider raises (endpoint
unreachable, HTTP error, every document scoring failed), the un-reranked hybrid
results are returned and exactly one warning is logged:

```
rerank_failed_open provider=self_hosted error_class=ConnectError context_id=... workspace_id=...
```

Alert on `rerank_failed_open` (a structured log event — the backend has no
metrics counters). `reranking_disabled_by_deployment` (debug) and
`reranking_disabled_by_plan_tier` (info) mark the two deliberate skips.

## Plan Tiers

Plans control resource limits per workspace. Defaults:

| Plan | Contexts | Memories | Memories/day | MCP calls/day | Owned workspaces |
|------|----------|----------|--------------|---------------|------------------|
| S (Free) | 1 | 1,000 | 50 | 1,000 | 1 |
| M (Basic) | 3 | 10,000 | 300 | 10,000 | 1 |
| L (Pro) | 20 | 100,000 | 2,000 | 50,000 | 3 |
| XL (Pro Max, key `promax`) | 1,000 | 100,000 | 10,000 | 250,000 | 20 |

The plan *key* (`free` / `basic` / `pro` / `promax`) is what the admin API,
the billing entitlement push and the `workspaces.plan_name` column use; the
size code is only its default display name.

**Memories/day** is a per-workspace quota on memory *creation* per UTC day
(counter in Redis, reset at 00:00Z; `resets_at` is returned with the 429).
It charges every path that creates a user-visible memory: MCP `remember`,
REST memory create, a brand-new `update_memory(external_id=...)`, and
connector / resource ingest (charged once per indexer batch, up front, for the
doc_ids not indexed yet — a re-sync of known docs is free, and a batch that
does not fit is left untouched and re-queued for the next UTC midnight). It
does **not** charge in-place updates (`update_memory` by id, `PATCH`), an
`external_id` replace, context merges, admin context recovery, or Sleep /
consolidation. If Redis is
down the check fails open. `0` follows the zero-floor rule used by every quota
field: the tier cannot create memories at all — it never means "unlimited".
Self-hosters who want no cap set a very large value.

"Owned workspaces" is a *per-user* cap, not a per-workspace one:
`cap = 1 + users.workspace_slot_bonus + owned_workspace_grant`, where the
grant (0 / 0 / 2 / 19) comes from the highest tier among the workspaces the
user owns. Admin/referral slot bonuses stack on top. Only creating another
workspace is gated — a user above the cap (e.g. after a downgrade) keeps
every workspace. Enforcement is behind `ENFORCE_WORKSPACE_CAP` (default
`false` = log-only); see `docs/ops/workspace-cap-rollback.md`.

Feature availability (the `features` set on each tier):

| Feature | S | M | L | XL |
|---------|---|---|---|----|
| Secret store (`secret_store`) | ✓ | ✓ | ✓ | ✓ |
| Shared contexts / team invitations / memory analysis | – | – | ✓ | ✓ |
| Memory analysis on the platform-managed LLM, no workspace key (`managed_llm`) | – | – | ✓ | ✓ |
| Resources — `setup_resource`, new resource tokens (`resources`) | – | – | – | ✓ |
| Connectors — `setup_connector` (`connectors`) | – | – | – | ✓ |
| Public — `set_public`, bound public API keys (`public_contexts`) | – | – | – | ✓ |

**Block-new-only rule.** The three XL-only rows gate *creation* only. A
workspace on M / L that already has resource tokens, connectors, public
contexts or bound public keys keeps them working end to end: the numeric caps
those objects serve against (`max_resource_tokens` 3 / 30, `max_connectors`
3 / 10, `public_calls_per_day` 1000 and `bound_public_calls_per_minute` 100
on L) are unchanged; only provisioning a *new* one is refused with a
`FEAT-001` / `plan_required` error naming the XL tier. Rotating
(regenerating) an existing bound public key is allowed on any tier: it revokes
the old key and mints its replacement against the same, still-public context,
so the number of bound keys does not grow. Because those M / L caps stay above
zero, an `extra_connectors` (or other resource) add-on on M / L still stacks
mechanically, but it cannot unlock creation — such an add-on only matters on
XL.

Override via environment variables (`PLAN_<KEY>_<FIELD>`, key upper-cased):

```bash
PLAN_FREE_MAX_CONTEXTS=5
PLAN_FREE_MEMORY_LIMIT=5000
PLAN_FREE_MEMORIES_PER_DAY=1000000   # effectively no daily cap (0 = none)
PLAN_BASIC_MAX_CONTEXTS=10
PLAN_PRO_MAX_CONTEXTS=50
PLAN_PROMAX_MAX_CONTEXTS=2000
PLAN_PRO_OWNED_WORKSPACE_GRANT=4      # L owners may own 1 + 4 = 5 workspaces
PLAN_PROMAX_OWNED_WORKSPACE_GRANT=49  # XL owners may own 50
```

### Feature set override (`PLAN_<KEY>_FEATURES`)

A tier's `features` set is env-overridable too. `PLAN_<KEY>_FEATURES` is a
comma-separated list (whitespace around names is ignored) that **replaces**
the tier's whole set — list every feature the tier should have, not just the
additions. Since #1551 only XL may *create* resources, connectors and public
contexts; a self-host that wants them on a lower tier re-enables them like so:

```bash
# M keeps its defaults (api_keys, oauth, reranking, managed_embeddings,
# secret_store) and may now also create resources / connectors / public contexts.
PLAN_BASIC_FEATURES=api_keys,oauth,reranking,managed_embeddings,secret_store,resources,connectors,public_contexts
```

Rules the API enforces when it loads the registry (a violation refuses to
start, naming the variable):

- Every name must be one of the known features (`api_keys`, `oauth`,
  `secret_store`, `reranking`, `managed_embeddings`, `managed_llm`,
  `team_invitations`, `shared_contexts`, `memory_analysis`, `resources`,
  `connectors`, `public_contexts`).
- **Invariants.** Every tier must keep `secret_store` (the zero-knowledge secret
  store is on every tier), and `resources` requires `public_contexts` —
  `setup_resource` creates a *public* context, so a tier that may create
  resources must also be allowed to make contexts public.

The "minimum tier" for each feature — what the `FEAT-001` refusal text and the
plan-comparison matrix name — is recomputed from the *effective* tiers (the
lowest tier that has the feature), so with the example above a free workspace
is told to upgrade to M, not XL. `allows_shared_contexts` follows
`shared_contexts` automatically. The effective set per tier is logged once at
startup (`plan_tier_features_effective`). The web UI honours the override too:
since #1560 its create gates for `resources`, `connectors`, `public_contexts`
and `shared_contexts` read the per-tier booleans from
`GET /api/v1/workspaces/plans/tiers` rather than ranking tier names
(`frontend/src/hooks/usePlanFeatures.ts`). Surfaces that still pre-check a tier
rank rather than the matrix are being migrated in #1645.

### What a feature entry does at runtime (enforcement modes)

**A feature appearing in the registry is not by itself evidence of a runtime
gate.** The tier mapping says which tier owns a feature; it does not say that
anything refuses a tier without it. Each entry therefore also declares an
*enforcement mode* (`config/plan_tiers.py`, `FEATURE_ENFORCEMENT`):

| Mode | What happens on a tier WITHOUT the feature |
| --- | --- |
| `enforced` | A runtime check **refuses** the request (`FEAT-001` / `plan_required` / a raised error), on every deployment. |
| `conditional` | A runtime check refuses **only where a deployment setting turns it on**; with that setting at its default the request is served anyway. |
| `degrades` | A runtime check exists, but the request **still succeeds** with reduced behaviour. Nothing is refused. |
| `advertised` | **No runtime check at all.** The entry exists so the plan pages can list the feature; every tier behaves the same. |

Current modes:

| Feature | Mode | Where |
| --- | --- | --- |
| `api_keys` | `advertised` | Nothing checks it — every tier may create API keys. |
| `oauth` | `advertised` | Nothing checks it — OAuth login and clients work on every tier. |
| `secret_store` | `advertised` | Nothing checks it, and every tier must keep it (invariant above), so the row can never be false. |
| `reranking` | `degrades` | Recall still returns results, just unreranked (`reranking_disabled_by_plan_tier` at info). |
| `team_invitations` | `enforced` | Creating an invitation is refused. |
| `shared_contexts` | `enforced` | A non-private context visibility is refused. |
| `public_contexts` | `enforced` | Publishing a context / minting a bound public key is refused. |
| `memory_analysis` | `enforced` | The analysis run is refused (403). |
| `managed_embeddings` | `conditional` | The platform-key embedding fallback is refused **only** where `EMBEDDING_PLATFORM_FALLBACK_REQUIRES_MANAGED_PLAN` is on; it defaults to off, so a default deployment embeds on the platform key on every tier. |
| `managed_llm` | `enforced` | Memory Analysis with no BYOK key is refused (`VAL-001`). |
| `resources` | `enforced` | `setup_resource` / new resource tokens are refused. |
| `connectors` | `enforced` | `setup_connector` is refused. |

The modes are a property of the **code**, not of a tier, so a
`PLAN_<KEY>_FEATURES` override does not change them — it only moves which tiers
carry which feature. `GET /api/v1/workspaces/plans/tiers` serves the map as
`feature_enforcement` on every tier row, so a UI can hard-disable a control for
an `enforced` feature and leave `conditional` / `degrades` / `advertised` ones
alone rather than inventing a gate the backend does not have. `backend/tests/config/test_feature_enforcement.py`
scans `backend/src` and fails when a declared mode and the real call sites drift
apart, so adding or removing a gate must update the mode in the same change.

The override changes *which* tiers may create; the numeric caps stay the
second gate, and several of them are 0 on the lower tiers with no env override
of their own:

- `max_resource_tokens` and `max_connectors` are 0 on Free — granting
  `resources` / `connectors` to Free still refuses at the count check, so grant
  them to M or above.
- `public_calls_per_day` and `bound_public_calls_per_minute` are 0 on Free
  **and M** — with the example above an M workspace can *create* a public
  context, but every public-API request against it is refused by the daily
  public quota, and no public-bound API key can be minted. To actually serve
  public traffic from the override, grant `public_contexts` (and `resources`)
  to L (`PLAN_PRO_FEATURES`) or above, whose public caps are non-zero.

Alternatively, for self-hosted single-user setups, simply assign the XL
(`promax`) plan to your workspace. Plan changes are **admin-only** by default. For SaaS deployments with self-service billing, enable Stripe:

```bash
BILLING_ENABLED=true
STRIPE_SECRET_KEY=sk_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_BASIC=price_xxx
STRIPE_PRICE_PRO=price_yyy
```

Plan display names in the web UI can be customized via `NEXT_PUBLIC_PLAN_FREE_DISPLAY_NAME` / `BASIC` / `PRO` / `PROMAX` (see [Frontend Environment Variables](#frontend-environment-variables)).

### Referral budget

The referral payout budget is the effective FREE → BASIC memory gap (`PLAN_BASIC_MEMORY_LIMIT − PLAN_FREE_MEMORY_LIMIT`), so lowering `PLAN_BASIC_MEMORY_LIMIT` — or otherwise narrowing the gap — shrinks it. When `ENABLE_REFERRALS=true`, the API refuses to start if `REFERRAL_MAX_GRANTS_PER_REFERRER × REFERRAL_REFERRER_REWARD_MEMORIES + REFERRAL_REFEREE_REWARD_MEMORIES` reaches the new gap (a fully-used referral chain would hand out the whole paid tier) — retune the `REFERRAL_*` values first.

## LLM & Embedding Pricing (cost tracking and spend caps)

Every token count the platform records — recall / remember embeddings, Sleep
and Memory Analysis LLM calls, reranking — is turned into USD by the
`llm_pricing` table: one append-only row per `(provider, model, unit_type)`
with a `price_per_unit`, a `unit_denominator` (1,000,000 = "per million
tokens") and an `effective_from`. The newest row whose `effective_from` is
before the call wins, so a price change never rewrites history. Alembic seeds
the OpenAI / Anthropic / Voyage / Cohere rate cards; the admin cost dashboard,
the per-workspace cost API and the embedding spend cap all read this table.

A model with **no** row is *unknown*, not free: its cost renders as `—`, the
call log marks `pricing_miss`, and the embedding spend cap does not apply.
This is deliberately the case for `self_hosted` models — a local Ollama is
free, but the same provider key also fronts paid OpenAI-compatible endpoints
(`SELF_HOSTED_BASE_URL` + `SELF_HOSTED_API_KEY`), and the platform cannot know
which one you run. (Earlier releases seeded those models at `$0`; that seed is
removed on upgrade so a paid endpoint no longer shows `$0.00`.)

### Setting prices (`LLM_PRICING_OVERRIDES`)

Price a model — self-hosted or any other — with a JSON array:

```bash
LLM_PRICING_OVERRIDES='[
  {"provider": "self_hosted", "model": "qwen3-embedding:4b",
   "unit_type": "embedding_tokens", "price_per_unit": 0.02},
  {"provider": "self_hosted", "model": "my-llm",
   "unit_type": "input_tokens", "price_per_unit": 0.5},
  {"provider": "self_hosted", "model": "my-llm",
   "unit_type": "output_tokens", "price_per_unit": 1.5}
]'
```

- `provider` / `model`: the names usage rows record. For embeddings `model`
  is the **registry** name (`qwen3-embedding:4b`), not the upstream id an
  alias in `SELF_HOSTED_MODEL_ALIASES` maps it to.
- `unit_type`: one of `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_write_tokens`, `embedding_tokens`, `rerank_tokens`,
  `rerank_search_units`. An LLM needs at least `input_tokens` and
  `output_tokens` for its cost to be known.
- `price_per_unit`: USD per `unit_denominator` units (default `1000000`, i.e.
  per million tokens; a vendor quoting per 1k tokens can pass
  `"unit_denominator": 1000`). Must be below `10000` with at most 10 decimal
  places (the `NUMERIC(14, 10)` column) — anything the column could not store
  as written is refused rather than rounded or dropped. Optional
  `context_min_tokens` for tiered rate cards.
- **USD only.** Every figure in the platform is USD; a vendor billing in
  another currency is entered at the converted USD price (and re-entered when
  the exchange rate moves enough to matter).

The value is validated when the API starts — a malformed entry refuses to
boot, naming `LLM_PRICING_OVERRIDES` and the entry index. Valid entries are
then written into `llm_pricing`: when the currently effective row for that
key already has the same price, nothing happens; otherwise a new row with
`effective_from = now` is appended (the old row stays for history). To apply
a change without restarting, run the same sync from the API environment:

```bash
python -m src.cli.sync_llm_pricing            # print what would be inserted
python -m src.cli.sync_llm_pricing --apply    # append it
```

Other API workers see the new price within the 60-minute price cache, or
immediately after a restart.

### Spend caps on self-hosted embeddings

`self_hosted` embeddings are capped **iff their effective price is above
zero**. With a price set, `PLAN_<KEY>_EMBEDDING_DAILY_CAP_USD` /
`_MONTHLY_CAP_USD` (and the per-workspace override, the 80% / 100% owner mails
and the `QUOTA-002` 429) apply exactly as they do to platform-paid OpenAI
embeddings, on every tier and regardless of BYOK. Unpriced (no row) or priced
at exactly `0` (an explicit "this local model is free") stays uncapped. Spend
counters only advance when the backend reports `usage` tokens: vLLM and
OpenAI-compatible servers do, a bare Ollama does not — a capped Ollama would
count nothing.

## LLM Credentials (BYOK vs platform-managed)

Two paid features call an LLM on the workspace's behalf: **Memory Analysis**
(cluster labelling) and **Sleep Maintenance** (the judge behind edge
discovery, dedup, importance re-evaluation and consolidation). Where the
credential comes from is decided per feature:

- **`ENABLE_BYOK`** (default `true`) controls key *provisioning* only: with
  `false`, the External Keys console's create/update paths, the workspace cost
  dashboard and the OpenAI key-status probe return 404 and the web UI hides
  them. It does **not** stop the LLM / embedding / reranker services from
  *resolving* keys that were stored before the flip — the owner deletes those
  through the still-open management paths, or the operator sets
  `RESOLVE_STORED_BYOK_KEYS=false` (below). Stored keys stay deletable either
  way: `OPENAI_API_KEY` is only protected ("Required") while `ENABLE_BYOK` and
  `RESOLVE_STORED_BYOK_KEYS` are both on *and* OpenAI embeddings are in use
  (`EMBEDDING_PROVIDER=openai`, or a live context of the workspace on an OpenAI
  embedding model) — see
  [Protected keys](api-reference.md#protected-keys-is_protected-issue-1613).
  With BYOK off the External Keys nav entry shows for the owner only while the
  workspace still stores a key (#1616); the page itself is always reachable at
  `/workspace/integrations/external-keys`.
- **Memory Analysis** was strict-BYOK: an enabled workspace OpenAI key had to
  exist, and the labelling calls refused the platform credential. Since
  #1569 the run resolves a *lane* instead (table below).
- **Sleep** resolves its judge key BYOK-then-env: a workspace key when one
  exists, else the platform credential for the configured provider.

### The platform-managed LLM lane (`MANAGED_LLM_*`)

```bash
MANAGED_LLM_PROVIDER=openai            # openai | anthropic | gemini | self_hosted
MANAGED_LLM_MODEL=gpt-5-nano            # the model id sent to that provider
# The matching platform credential must be present at boot:
#   openai → OPENAI_API_KEY, anthropic → ANTHROPIC_API_KEY, gemini → GOOGLE_API_KEY,
#   self_hosted → SELF_HOSTED_BASE_URL (+ SELF_HOSTED_API_KEY if the server needs one)
```

When set, a workspace whose plan carries the **`managed_llm`** feature (L / XL
by default; grant it to any tier with `PLAN_<KEY>_FEATURES`) can run Memory
Analysis with **zero** external keys. The run records `paid_by='platform'`,
the labelling calls use the platform credential only (a workspace's stored
key is never billed by accident), and the chain is the single managed model —
no cross-provider fallback. Sleep's judge also defaults to this pair when no
explicit `SLEEP_LLM_PROVIDER` / `SLEEP_LLM_MODEL` is set (a `neural_config`
row still wins). A half-configured lane (provider without model, missing
platform credential, `self_hosted` without an explicit `SELF_HOSTED_BASE_URL`)
refuses to boot, naming the variable. `GET /api/v1/system/info` exposes
`features.managed_llm` so the web UI knows a workspace key is not required.

**Lane resolution for a Memory Analysis run** (`services/analysis/llm_lane.py`):

| BYOK provisioning | Enabled workspace OpenAI key | `MANAGED_LLM_*` set | Plan has `managed_llm` | Lane |
|---|---|---|---|---|
| on | yes | any | any | **BYOK** — strict, OpenAI chain, `paid_by='byok'` (unchanged) |
| any | no (or BYOK off) | yes | yes | **managed** — platform credential only, `paid_by='platform'` |
| any | no (or BYOK off) | yes | no | refused (`VAL-001`), message names the plan feature |
| any | no (or BYOK off) | no | – | refused (`VAL-001`), message names both routes |

**Cost of the managed model.** Analysis needs no `llm_pricing` row for it —
an unpriced model runs with `memory_analyses.model_id = NULL` and
`cost_estimated_cents` / `cost_actual_cents` = `NULL` ("cost unknown", the
same posture #1570 gave unpriced embeddings). To track spend, price it with
`LLM_PRICING_OVERRIDES` (previous section); `/preview` and the run then quote
the same rate card. The REST `model_id` (an `llm_pricing` row to pin) is
honoured on the BYOK lane only — on the managed lane both `/preview` and
`start` refuse it with `VAL-001`, since the snapshot and cost attribution
must name the model the lane actually runs.

### Recipe: hosted deployment with no BYOK at all

```bash
ENABLE_BYOK=false                        # no key provisioning, no cost dashboard
RESOLVE_STORED_BYOK_KEYS=false           # optional hardening, see below
MANAGED_LLM_PROVIDER=self_hosted         # or openai / anthropic / gemini + its key
MANAGED_LLM_MODEL=qwen3:8b
SELF_HOSTED_BASE_URL=http://vllm:8000    # explicit, even if it equals the default
SELF_HOSTED_API_KEY=                     # if the server was started with --api-key
SELF_HOSTED_MODEL_ALIASES=qwen3:8b=Qwen/Qwen3-8B-Instruct   # wire id, if different
SELF_HOSTED_LLM_TIMEOUT_SECONDS=120      # a local model may need more than 60 s
LLM_PRICING_OVERRIDES='[{"provider":"self_hosted","model":"qwen3:8b","unit_type":"input_tokens","price_per_unit":0.05},{"provider":"self_hosted","model":"qwen3:8b","unit_type":"output_tokens","price_per_unit":0.20}]'
# PLAN_FREE_FEATURES=api_keys,oauth,secret_store,managed_llm   # to open it to every tier
```

Notes for `self_hosted` as the managed LLM: the `SELF_HOSTED_MODEL_ALIASES`
mapping applies to chat completions as well as embeddings (#1569); a backend
that rejects OpenAI's `response_format` (HTTP 400) gets one retry without it
and the JSON contract then rests on the prompt plus `LLMService`'s parse
retry; judge failures still count toward a Sleep report's `degraded` grade.

### Stop resolving stored keys (`RESOLVE_STORED_BYOK_KEYS=false`)

Opt-in hardening for a deployment that turned BYOK off and wants "off" to
mean off: `LLMService`, `EmbeddingService` and `RerankerService` skip their
`external_api_keys` lookups and use the platform env / settings credential
only (for `self_hosted`, `SELF_HOSTED_BASE_URL` — never a workspace's stored
URL). `EmbeddingService`'s BYOK existence probe (the spend-cap plan gate,
`paid_by` attribution and the shared-context read preflight) treats stored
keys as absent too, so a Free workspace is refused the platform fallback
rather than slipping through on an ignored key. Logged once per process as
`byok_key_resolution_disabled`. Requires
`ENABLE_BYOK=false` (refused at boot otherwise: users must not be able to
store keys the services ignore). Default `true` keeps the #1167 behaviour.
With it set, Sleep's judge calls on the managed lane also pass
`platform_only`; without it Sleep keeps BYOK-then-env. The ignored rows are
not deleted for you: each workspace owner can still list and delete their own
stored keys (#1613).

## Redis Connection Pool (rate limits and daily quotas) — Issue #1556

Per-minute rate limits and the daily MCP / REST / Public API quotas above are counters in Redis, incremented by `RateLimitMiddleware` on every authenticated request. The singleton client (`backend/src/db/redis.py`) uses a `BlockingConnectionPool`: when all pooled connections are checked out, a request waits up to `REDIS_POOL_TIMEOUT_SECONDS` (default `2.0`) for one to free up instead of failing immediately. Size the pool with `REDIS_MAX_CONNECTIONS` (default `50`, per API worker process). Quota checks are **fail-open** by design — if Redis is down, or the pool wait times out, the request is allowed and the counter update is lost (or, if Redis fails between the `INCR` and the `EXPIRE` that follows it, only partially applied). Each such miss logs exactly one `quota_check_failed_open` warning with `quota` (`per_minute`, `daily_mcp`, `daily_public`, `daily_rest`), `key_prefix` and `error_class` fields. Alert on that event: a sustained stream of it means quotas are not being enforced and either Redis or the pool size needs attention.
