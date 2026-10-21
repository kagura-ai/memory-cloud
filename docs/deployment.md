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
  and keep credentials out of it — invite tokens (see
  [Closed-beta invite links](#closed-beta-invite-links-issue-1581) and
  [Workspace invitation links](#workspace-invitation-links)) and the
  `X-Resource-API-Key` header (see
  [Credential headers in the proxy log](#credential-headers-in-the-proxy-log)) —
  the single-server template's `Caddyfile.tpl` does all of this

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
declares a `json-file` logging block, so the stack is bounded on any host — not
only on one whose Docker daemon was configured for it. With the knobs below
left unset, every service renders these defaults:

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"   # default: rotate at 50 MB ...
    max-file: "3"     # default: keep 3 files, at most ~150 MB per container
```

For Caddy that is still enough access log for incident triage; ship the logs
elsewhere if you need longer retention.

#### Changing the cap — Issue #2025

The two values are read from `.env.prod` (the `--env-file` `deploy.sh` always
passes), with the API colors on their own pair so that a longer API retention
does not grow the web, proxy and data-tier logs. Unset, every service renders
exactly the block above.

| Variable | Default | Applies to |
|---|---|---|
| `API_LOG_MAX_SIZE` | `50m` | `api-blue` / `api-green`: size at which the log file rotates |
| `API_LOG_MAX_FILE` | `3` | `api-blue` / `api-green`: rotated files kept |
| `LOG_MAX_SIZE` | `50m` | every other service (`web`, `caddy`, `postgres`, `qdrant`, `redis`, `ollama`) |
| `LOG_MAX_FILE` | `3` | the same services |

Accepted formats: a size is a whole number with a lowercase `k`, `m` or `g`
unit (`^[1-9][0-9]*[kmg]$`, e.g. `200m`); a file count is a positive whole number
(`^[1-9][0-9]*$`). An empty value means the default. `deploy.sh` checks the
values it will pass (the shell environment first, then `.env.prod`) and stops
before it touches a container; Docker itself would reject a bad value only when
it creates the container. The API writes one line per request plus the
structured events operators tally (scheduled-job results, `supersede_verify_*`),
so its logs are the first to rotate out; raising `API_LOG_MAX_*` keeps them
longer. Setting `log-opts` in `/etc/docker/daemon.json` does not change any of
this: a compose `logging:` block overrides the daemon default.

- **Takes effect only when a container is recreated** — `docker compose
  restart` keeps the old options, as described below.
- **Blue-green recreates only the incoming color.** The next `deploy.sh` run
  applies `API_LOG_MAX_*` to the color it deploys to; the other color follows
  with the deploy after that. `caddy` and `web` (and the data tier) pick up
  `LOG_MAX_*` only through their own recreate (the commands below).
- **A reboot recreates whatever is pending.** The systemd unit `startup.sh`
  installs (`kagura-memory.service`, once enabled) runs a plain
  `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d` at
  boot, and a plain `up -d` recreates every service whose configuration
  changed — `caddy`, `web` and the data tier included, with nobody watching. A
  changed `LOG_MAX_*` therefore means the next reboot restarts `postgres`,
  `qdrant` and `redis`. Apply it deliberately: right after setting it, recreate
  those services yourself in a maintenance window (commands below), and do not
  leave the change pending. The same recreate moves `qdrant` to the image the
  compose file pins, so the
  [Qdrant upgrade runbook](ops/qdrant-upgrade-runbook.md) caveat in the table
  below applies to a reboot too. Setting only `API_LOG_MAX_*` and leaving
  `LOG_MAX_*` unset changes no data-tier configuration, so nothing restarts
  the databases.
- **Plan the disk for both colors.** A stopped standby color keeps its log
  files until it is recreated, so the API takes up to
  `2 × API_LOG_MAX_SIZE × API_LOG_MAX_FILE`, plus
  `LOG_MAX_SIZE × LOG_MAX_FILE` for each other service.
- **Split host:** the app VM's `docker-compose.app.yml` reads all four; the
  data VM's `docker-compose.data.yml` reads `LOG_MAX_*` from the env file passed
  there, and its copy of the file is updated by hand (as for
  [Issue #1980](#rolling-it-out)).

Check a recreated container with
`docker inspect -f '{{json .HostConfig.LogConfig}}' kagura-api-blue` (or
`kagura-api-green`, `kagura-caddy`, ...).

**Who made an MCP request.** Since #2025 the per-request
`MCP auth success: method=…, user=…` line is logged at DEBUG, so the INFO log
no longer names the principal of each MCP request (failed auths still log at
WARNING). Since #2032 the rest of the per-request transport lines are DEBUG as
well: `MCP request`, `MCP authenticated`, `MCP session_id from …`,
`MCP OAuth2 workspace switch`, `MCP handling`, `using existing session`, the
per-method `MCP tools/list` / `tools/call` / `calling tool` / `ping` /
`server/discover` / `batch` / `notification` lines, and the stateless-era
`MCP '<method>' (stateless, <revision>): client=…` line. So the INFO log no
longer names the tool a successful call ran, nor which client and protocol
revision a stateless request used; `LOG_LEVEL=debug` brings those back too.
Session lifecycle (`initialize`, created, terminated, re-adopted, ended by
`DELETE`), refusals, failed auths and errors keep their levels, as do
`mcp_guardrail_digest_served` / `mcp_guardrails_param_ignored` (on
`initialize` and `server/discover`) and `mcp_tool_profile_names_ignored` (a
`?tools=` value naming an unknown tool). For incident tracing, agent-bound requests leave an append-only
`memory_access_events` row (user, principal type, API key prefix, agent,
context, operation, outcome; see
[memory_access_events retention](ops/memory-access-events-retention.md)), and
`api_keys.last_used_at` shows when each API key was last used (written at most
once per `API_KEY_LAST_USED_THROTTLE_SECONDS`, default 60). OAuth and
session-cookie requests have no per-request row; set `LOG_LEVEL=debug` in
`.env.prod` and recreate the API colors to bring the per-request line back,
along with every other DEBUG line, while you investigate. DEBUG also logs
token and session-id prefixes, so set `LOG_LEVEL` back and recreate the API
colors again when you are done.

**Applying it to a running stack.** `logging` options are fixed when a container
is *created* — `docker compose restart` does not apply them. After updating to a
release that carries the block:

| Service | How it picks the option up |
|---|---|
| `api-blue` / `api-green` | `deploy.sh` recreates the color it deploys to on every run — nothing to do (the other color follows with the next deploy). The same holds for a changed `API_LOG_MAX_*`. |
| `web` | Recreated by `deploy.sh --web`, or once by hand (below). |
| `caddy` | **Never recreated by `deploy.sh`** (it only restarts Caddy) — recreate it once by hand (below). |
| `postgres` / `qdrant` / `redis` (and `ollama`) | Recreating them **restarts the database**, so do it in a maintenance window. Their logs are small; it can wait for the next planned one. Recreating `qdrant` also moves it to the image the compose file pins: a volume from before v0.87.0 must first be upgraded one minor at a time ([Qdrant upgrade runbook](ops/qdrant-upgrade-runbook.md)), or Qdrant crash-loops. |

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

# Data tier — maintenance window only (volumes are kept, the services restart).
# Qdrant older than 1.18? Run docs/ops/qdrant-upgrade-runbook.md first.
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

### Credential headers in the proxy log

Caddy's JSON access log writes **every request header** by value, and Caddy
redacts only `Cookie`, `Set-Cookie`, `Authorization` and `Proxy-Authorization`
on its own. Any other header that carries a credential has to be removed by the
site's log configuration — and removed a second time in the global `log default`
block, because a request whose upstream failed (a `502` while the API is being
restarted or deployed) is written again as an `http.log.error` line through
Caddy's default logger, request headers included.

The single-server `Caddyfile.tpl` drops these request headers in both blocks:

| Header | Why |
|---|---|
| `Cookie` | session material |
| `Referer`, `Next-Router-State-Tree`, `Next-Url` | may repeat an invite URL ([Closed-beta invite links](#closed-beta-invite-links-issue-1581)) |
| `X-Resource-Api-Key` | the **resource token** an ingester authenticates `POST /api/v1/resources/{resource_id}/events` with ([Resource Tokens Guide](resource-tokens-guide.md)). Resource tokens do not expire, so a logged value stays usable until the token is revoked. |

Two details matter if you run a different proxy or edit the template:

- **Header-name spelling.** Caddy logs header names in Go's canonical form and the
  `filter` encoder looks the field up case-sensitively. The delete line has to
  read `request>headers>X-Resource-Api-Key delete`; a line spelled the way the
  API documents the header (`X-Resource-API-Key`) validates and matches nothing.
  `terraform/single-server/scripts/tests/caddy_log_scrub_live.bats` proves the
  exact line against the real image.
- **Both loggers.** Turning the access log off is not enough: the error line
  still carries the headers. The `log default` block must carry the same deletes.

**Applying a template change** needs no container recreate: re-render and
restart Caddy, or run a normal `deploy.sh`:

```bash
cd /opt/kagura-memory/src/terraform/single-server
./scripts/deploy.sh --generate-caddyfile \
  && docker compose -f docker-compose.prod.yml --env-file .env.prod restart caddy
```

**If the proxy logged the header before the delete was in place**, treat every
resource token that ingested through that proxy while those log lines were kept
as exposed:

1. Create a replacement token for each affected resource — creating a new token
   does **not** invalidate the old one — and switch the ingester to it:

   ```bash
   curl -X POST https://<your-domain>/api/v1/resource-tokens \
     -H "Authorization: Bearer kagura_{your_api_key}" \
     -H "Content-Type: application/json" \
     -d '{"resource_id": "<resource_id>", "context_id": "<context-uuid>", "description": "rotated"}'
   ```

2. Revoke the old token once the ingester uses the new one:

   ```bash
   curl -X DELETE https://<your-domain>/api/v1/resource-tokens/<rtok_old_id> \
     -H "Authorization: Bearer kagura_{your_api_key}"
   ```

   Both steps are also available in the Web UI under **Integrations → Resource
   Tokens**.

3. Discard the Caddy container's existing log file by recreating the container
   (`:80`/`:443` drop for a few seconds; add `--no-build` on a registry-mode
   host), and delete any copies that were exported or shipped elsewhere:

   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.prod \
     up -d --no-deps --force-recreate caddy
   ```

Until then, restrict who can read the container logs (root and the `docker`
group on the host).

## Production secrets (Issue #2225)

With `ENVIRONMENT=production` (hard-coded in
`terraform/single-server/docker-compose.prod.yml`), `Settings` refuses to load
while any of these three is still a shipped placeholder — the built-in default,
or the `change-me-…` / `kagura-erasure-v1` value from `.env.example` or
`terraform/single-server/.env.prod.example`:

| Variable | Used for | Minimum length |
|---|---|---|
| `API_KEY_SECRET` | Fernet encryption of stored provider keys and connector secrets | 32 |
| `AUDIT_HMAC_KEY` | Keyed hashes in audit rows, known-device cookies, consent CSRF token, secret-store audit chain | 32 |
| `AUDIT_PSEUDO_SALT` | Erasure pseudonyms (`sha256(salt + identifier)`) | none (placeholder check only) |

Placeholders are matched case-insensitively. Generate each value with
`openssl rand -hex 32`. The API loads the settings on import, so it stops
before serving with a pydantic `ValidationError` whose message names the
refused variables (never their values); look for it in
`docker compose logs api`. `alembic upgrade` (so `deploy.sh` stops at its
migration step, before traffic moves) and the admin CLIs (`create_admin`,
`reset_password`, `delete_admin`, `seed_e2e_admin`) are refused the same way,
before they touch the database. The one-shot maintenance commands
(`restore_context`, `sweep_orphan_vectors`, ...) load the settings when they
open their database session and stop there, with the same message in a
traceback.
Development, test and staging are not checked.
`ENVIRONMENT=development` is not a free way around it: it also drops the
`Secure` flag on session cookies.

**Rotate only what is still a default.** A placeholder must go; a private value
is left alone — except a private `API_KEY_SECRET` or
`AUDIT_HMAC_KEY` shorter than 32 characters, which is refused too. Replacing
any value that is already private has side effects:

- `AUDIT_HMAC_KEY`: every known-device cookie hash stops matching, so every
  user gets a new-device notice unless `user_known_devices` is cleared and
  `users.known_devices_since` is reset in the same step; `verify_audit_chain`
  reports a mismatch on every secret-store audit row written before the
  change; consent pages open at that moment answer 400; email HMACs in audit,
  signup-gate and identity-link rows stop being comparable with new rows.
  Moving off the public default is still required — do it once, in a
  maintenance window.
- `AUDIT_PSEUDO_SALT`: nothing is looked up by pseudonym, but rows
  pseudonymized before and after the change for the same person no longer
  correlate. Change it only if it is still the public default; a short private
  salt is accepted.
- `API_KEY_SECRET`: every stored Fernet ciphertext (BYOK provider keys,
  connector secrets, worker app identities) becomes undecryptable, so stored
  provider keys have to be entered again.

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

## Workspace invitation links

A workspace invitation (the `/workspace/members` page, or
`POST /api/v1/workspaces/{workspace_id}/invitations`) is a one-time link,
`{FRONTEND_URL}/invite/{token}`, bound to the invited e-mail address and valid
for 7, 30, 90 or 365 days, or until used. Like a closed-beta link, its token
travels in URLs: the landing page `/invite/{token}`, the unauthenticated preview
`GET /api/v1/invitations/{token}` the page calls, and — when the invitee has to
sign in first — the landing URL percent-encoded in the OAuth login's
`return_to` (`…/login?return_to=https%3A%2F%2F<host>%2Finvite%2F{token}`), which
the callback then repeats in its `Location` header.

The same scrub as for closed-beta links covers these three shapes: structured
logs, the uvicorn access log and the `usage_stats.endpoint` column record the
literal `{token}`, and the single-server `Caddyfile.tpl` writes `REDACTED` into
the token slot of the request URI (both loggers) and of the `Location` /
`Refresh` response headers. `/api/v1/invitations/accept`, `/api/v1/invitations/pending`
and `/api/v1/workspaces/{id}/invitations/{invitation_id}` carry no token and are
recorded as they are. **A different reverse proxy has to scrub these shapes
too** — the list of copies to cover is the one given for closed-beta links
above.

Upgrading from a release that did not scrub these shapes:

1. **Purge or redact the existing log files.** Lines already written are not
   rewritten. On the single-server template, empty the current log file of the
   proxy and of both API colours without a restart (Docker's `json-file` driver
   appends, so the containers keep logging):

   ```bash
   for c in kagura-caddy kagura-api-blue kagura-api-green; do
     sudo truncate -s 0 "$(docker inspect --format '{{.LogPath}}' "$c")"
   done
   ```

   Rotated files next to it (`<LogPath>.1`, `.2`) hold older lines — remove
   them the same way, or recreate the containers (`docker compose
   -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps
   --force-recreate caddy`; the next `deploy.sh` run recreates the API colours).
   If the logs are shipped elsewhere, redact the token slot in the aggregator
   as well: anything after `/invite/`, `/api/v1/invitations/` (other than
   `accept` and `pending`) or `%2Finvite%2F`.

2. **Redact the `usage_stats` rows** written for the preview call of a
   signed-in visitor. Rewriting the endpoint to the shape the scrub now writes
   keeps the usage counts; the two token-free routes are left alone:

   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.prod exec postgres \
     psql -U kagura -d kagura -c "
       UPDATE usage_stats
          SET endpoint = '/api/v1/invitations/{token}'
        WHERE endpoint LIKE '/api/v1/invitations/%'
          AND endpoint NOT IN ('/api/v1/invitations/accept',
                               '/api/v1/invitations/pending',
                               '/api/v1/invitations/{token}');"
   ```

   The statement is idempotent — run it once per database.

3. **Optionally, revoke and re-issue the pending invitations** whose links were
   in those logs: the `/workspace/members` page, or
   `DELETE /api/v1/workspaces/{workspace_id}/invitations/{invitation_id}` for
   each entry of `GET /api/v1/workspaces/{workspace_id}/invitations`, then
   invite again. A token on its own does not join the workspace — accepting
   needs a session signed in as the invited address — so this is a precaution,
   most relevant for invitations with a long or no expiry.

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
  the parameter. An invite sign-up (`/join/{token}`) that lacks the current
  version is stopped earlier, at the login endpoint, and sent back to
  `/join/{token}?error=terms_required` so the invite still works; the callback
  only holds the invite's hash, so a refusal there (the version changed during
  the few seconds at the provider) ends on `/login`. An identity whose e-mail
  already belongs to another account is not a new sign-up here: it keeps the
  `email_in_use` answer. Password login never creates accounts; accounts
  created with the admin CLI are asked on first sign-in like any existing user.
- The sign-in buttons on `/login`, `/join/{token}` and the invitation page stay
  disabled until `/api/v1/system/info` has answered once; if it fails they
  unlock without a version (the pre-#1665 behaviour).
- **Existing users are never locked out.** A sign-in with a missing or older
  version succeeds. The web UI then sees `terms_acceptance_required: true` on
  `GET /api/v1/auth/me` and shows a blocking "updated terms" dialog; accepting
  calls `POST /api/v1/me/terms-acceptance` (the user can sign out instead). The
  workspace invitation page, which sits outside the app layout, shows the same
  dialog before it accepts an invitation for a signed-in user.
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

## Email + password sign-in (Issue #1678)

Existing accounts can sign in with a verified email address and a password,
in addition to Google / GitHub. **No endpoint creates an account**: new
accounts still come from an OAuth sign-in (through the signup gate and beta
invites) or the admin CLI, so this adds no way around a closed registration.

**Who can sign in by email.** `POST /api/v1/auth/login` keeps its `login_id`
field and reads it as either a login ID or an email address:

1. an exact `login_id` match on an account with a password (the CLI admins,
   unchanged, MFA included);
2. otherwise, for a value containing `@`, the account whose email equals it
   case-insensitively (`lower(trim())`), whose `email_verified_at` is set and
   which has a password. `@local` addresses never match. When two accounts'
   emails differ only by case the lookup fails closed (a generic 401).

`email_verified_at` is set when a person proves the mailbox by following a
set-a-password link. The e86 migration back-fills it for accounts with a linked
Google / GitHub identity (those addresses came verified from the provider) —
never for `@local` addresses. `users.auth_method` is unchanged and still means
the *original* sign-in method; whether an account has a password is
`password_hash IS NOT NULL` (`GET /api/v1/auth/me` reports it as
`has_password`). Unlinking a provider, removing the password and the account
erasure flow (password re-entry vs emailed link) all follow `has_password`. For
erasure it is read when the erasure is requested: an emailed confirmation link
stays valid on its own even if a password is set before it is clicked.

Every sign-in failure is the same 401, and an unknown identifier costs the same
bcrypt work as a wrong password. Failures are counted per account (5 per 5
minutes) — an account's login ID and its email share one budget, and a success
resets it; an identifier that names no account is counted on its own
(normalized). There is no per-client-address lockout: behind a reverse proxy
that does not forward the client address it would lock everyone out.

With MFA on, the second factor draws on the same budget: a wrong TOTP code at
`POST /api/v1/auth/mfa/verify` counts like a wrong password, a correct password
does not reset the counter, and the budget spent both endpoints answer `429` —
only the completed sign-in clears it. A code that signed the account in is not
accepted again, and the wrong code that spends the budget emails the account
owner (a wrong password does not: anyone who knows a login ID can send one).
Deployments that also rate-limit `/api/v1/auth/login` and
`/api/v1/auth/mfa/verify` per client address at the reverse proxy may keep
doing so; the API's counter is per account and ignores the client address.

**Links and endpoints.**

| Endpoint | Auth | What it does |
|----------|------|--------------|
| `POST /api/v1/auth/password/reset-request` `{email}` | public | Always `202` with the same body. Emails a reset link only to an account with that verified email and a password. 10 per client address and 3 per address per 15 minutes (the per-address limit is silent). |
| `POST /api/v1/auth/password/reset` `{token, new_password}` | the link | Sets the password, **signs out every browser session** and **revokes every OAuth / MCP grant** of the account (see below); the person then signs in. |
| `POST /api/v1/me/password/setup-request` | browser session | For an account without a password: emails a set-a-password link to the account's address (`409` if it has one, `400` for `@local`). |
| `POST /api/v1/auth/password/setup` `{token, new_password}` | the link | Sets the first password, marks the email verified, signs out the account's other browser sessions. |
| `POST /api/v1/me/password/change` `{current_password, new_password}` | browser session | Signs out the account's other browser sessions. |
| `DELETE /api/v1/me/password` `{current_password}` | browser session | Refused (`409`) while no Google / GitHub identity is linked — the last sign-in method can never be removed. |

Links are single-use and expire (`PASSWORD_RESET_TOKEN_TTL_MINUTES`,
`SET_PASSWORD_TOKEN_TTL_MINUTES`, default 30; `VERIFY_EMAIL_TOKEN_TTL_HOURS`,
default 24, reserved for a later verification flow). Only a SHA-256 digest is
stored (`email_action_tokens`); a newer link for the same purpose invalidates
the older one, and a link stops working if the account's email changed after it
was sent. Links point at `FRONTEND_URL` (`/password/reset?token=…`,
`/password/setup?token=…`); those pages send `Referrer-Policy: no-referrer` and
are not indexed. New passwords follow the admin CLI policy (12+ characters,
upper- and lower-case letter, digit, symbol, at most 72 bytes).

A **reset** is the recovery path after a compromise, so it also revokes, in
the same transaction as the new password (#1738): every OAuth / MCP access and
refresh token issued to the account, authorization codes not yet exchanged,
and device-flow codes. Connected clients (Claude Code, ChatGPT, the CLI) must
sign in again. A set-up, change or removal signs out browser sessions only.

API keys, OAuth client secrets, share keys and resource tokens are **not**
revoked by any password flow: they are integration credentials, and revoking
them would silently break integrations. The reset email and the reset page tell
the person to review them in Settings. Used and expired `email_action_tokens`
rows are deleted by an hourly job once they are
`EMAIL_ACTION_TOKEN_RETENTION_SECONDS` (default `86400`) past use or expiry.

**New-device sign-in alerts.** A browser sign-in from a device the account has
not used before emails the owner (same pipeline and mandatory like the other
security notices). The device is a long-lived `kagura_device` cookie whose
keyed HMAC is stored in `user_known_devices`; no IP address or user agent is
stored there (the IP and user agent go into the email and, while a notice is
coalesced or retried, into the notice queue in Redis). A daily
job forgets devices not seen for `KNOWN_DEVICE_RETENTION_DAYS` (default `180`),
and at most `KNOWN_DEVICE_MAX_PER_USER` (default `20`) devices are kept per
account. After the upgrade every account's next sign-in registers its browser
silently (no known device yet), so the alerts start with the second browser.

**Email delivery.** The links need `EMAIL_PROVIDER=resend`. The default
`logging` provider writes one `email_dispatch_required=true` line per email with
the purpose and a keyed hash of the recipient — never the address, the token or
the link — so under `logging` the links cannot be delivered and self-service
reset / set-up do not work.

**Security-change notifications (Issue #1752).** After a password is set,
changed, reset or removed, a Google / GitHub identity is linked or unlinked, an
OAuth / MCP client is authorized (by consent when it grants something new — a
first authorization, a broader scope, or a client changed since the last grant
— and by device-flow approval every time), an API key is created or regenerated
(connector write keys included), an OAuth client is registered or its secret
regenerated, a provider sign-in changes the account's email address (the
previous address is told), or a password sign-in with MFA fails the second
factor until the account's sign-in budget is spent, the account owner is
emailed a notice (UTC time, IP address, user agent, key or client name; for an
admin action the acting admin, without the admin's IP address and user agent —
never a secret, token or link other than the plain `FRONTEND_URL/profile`
page). The notices cannot be turned off. They go
only to a verified address (`users.email_verified_at`: set by an emailed
password link, or by an OAuth sign-in whose provider attests the address as
verified; migration `e88_1752_verified_backfill` marks the OAuth accounts
created before sign-in set it), never to
`@local`. The first three occurrences of the same event for the same account
within `SECURITY_NOTIFICATION_WINDOW_SECONDS` (default 600, at most 3600) are
each sent at once; later ones are sent as one digest when the window closes.
The window lives in
Redis and a job checks it every minute; when Redis is unavailable every
occurrence is sent at once. A digest keeps the first 20 occurrences and counts
the rest; an email whose send definitely fails — a notice or a digest — is
retried up to twice (after one, then two minutes), then dropped with a
`security_notification_digest_dropped`
log line. A send whose answer never came may still be delivered, so it is not retried.
Pending windows are kept in Redis for 7 days, so a stalled job loses nothing
that recent; a window older than that is dropped with a
`security_notification_window_expired` warning. Operator
CLI actions (`reset_password`, `create_admin`) send no notice. A send failure is logged and never affects the
change. Under `EMAIL_PROVIDER=logging` each notice is one
`security_notification_email` log line (event and a keyed recipient hash only).

**Removed sign-in providers.** A Google / GitHub identity signs in to an
account only through its `user_oauth_providers` row. Once the owner removes
that provider from **Settings** (`POST /api/v1/me/account/unlink-provider`),
the identity has no row and owns nothing — including the account it created,
whose `users.user_id` still equals the provider's subject. A sign-in through
it is refused: the callback redirects to `/login?error=provider_unlinked`,
opens no session, and the backend logs
`oauth_unlinked_identity_sign_in_refused` (or `session_owner_not_found`).
Earlier releases let such an identity back in by the `users` row alone. After
upgrading, list the accounts in that state and review their recent activity —
`users.last_login_at`, the browsers in `user_known_devices`, and their
`audit_logs` rows (`oauth_provider_unlinked`, `password_set`, and anything
that follows the unlink: `oauth_provider_linked`, `api_key_created`,
`password_changed`, role changes):

```sql
-- OAuth-created accounts whose original identity has no provider row
SELECT u.user_id, u.email, u.auth_provider, u.last_login_at
FROM users u
WHERE u.auth_method = 'oauth'
  AND NOT EXISTS (
    SELECT 1 FROM user_oauth_providers p WHERE p.oauth_sub = u.user_id
  )
ORDER BY u.last_login_at DESC NULLS LAST;

-- One account's recent sign-in devices and audit trail
SELECT first_seen, last_seen FROM user_known_devices WHERE user_id = '<user_id>' ORDER BY last_seen DESC;
SELECT created_at, action, resource, ip_address FROM audit_logs WHERE user_id = '<user_id>' ORDER BY created_at DESC LIMIT 50;
```

A sign-in dated after the `oauth_provider_unlinked` row that was not the
owner's is a reason to reset the account's password (which signs it out
everywhere and revokes its OAuth / MCP grants, see above) and to review its
API keys and OAuth clients in Settings.

## Which sign-in provider syncs the account's email (Issues #1811, #1875)

`users.auth_provider` names the account's **primary** sign-in provider. Only
a sign-in through it syncs the account's email and name from the identity
provider; a sign-in through any other provider on the **Connected accounts**
card updates nothing but that provider's last-used time.

An OAuth account can have no primary provider (`auth_provider` NULL): a row
from before the pointer existed, or an account that set a password and then
unlinked its last provider. Such an account gets the pointer back on a
sign-in through **the identity it was created with** — the Google / GitHub
identity whose `sub` is the account's `user_id` — once that identity's link
is older than 10 minutes or was written together with the account. A
provider attached later through **Connected accounts** never becomes primary,
however long ago it was attached: attaching needs only a live session, so a
session alone can never choose whose email and name the account takes. Such
a sign-in logs `oauth_primary_provider_not_adopted_attached_identity` and
goes through as a linked sign-in. Unlinking the primary provider follows the
same rule: the pointer moves to the identity the account was created with
when that one is still attached, and is cleared otherwise — never to a
provider attached later. (A password account's pointer still moves to any
surviving provider, as before.)

**After upgrading**, review the accounts whose primary provider is a later
attached identity. From this version on the pointer gets there only by hand;
before it, it could land there when the original provider was unlinked, which
owners did legitimately, so this is a list to check with the owners, not a
list of mistakes. On the database host:

```bash
docker exec kagura-postgres psql -U kagura -d kagura -c "
  SELECT u.user_id, u.auth_provider, p.linked_at
  FROM users u
  JOIN user_oauth_providers p
    ON p.user_id = u.user_id AND p.provider = u.auth_provider
  WHERE u.auth_method = 'oauth' AND p.oauth_sub <> u.user_id
  ORDER BY p.linked_at DESC;"
```

To point an account at a provider the owner confirms, or to clear the pointer
so that no provider syncs the profile until the original identity signs in:

```bash
docker exec kagura-postgres psql -U kagura -d kagura -c \
  "UPDATE users SET auth_provider = 'google' WHERE user_id = '<user_id>';"
docker exec kagura-postgres psql -U kagura -d kagura -c \
  "UPDATE users SET auth_provider = NULL WHERE user_id = '<user_id>';"
```

Setting the pointer by hand is also the way to make a later attached
provider primary for an owner who asks for it; neither a sign-in nor an
unlink does.

## One person, two accounts — identity links (Issue #1784)

Two different things are called "linking" in Kagura, and they do not overlap:

- **Sign-in providers** (OpenAPI tag `account-linking`, Issue #517) attach
  another way to sign in — Google, GitHub — to **one** account. It is still
  one `user_id`.
- **Identity links** (OpenAPI tag `identity-links`, this section) count
  **separate accounts**, each with its own sign-in and its own `user_id`, as
  one owner of their private contexts.

Identities are keyed by `user_id` and are never linked by email: a CLI admin
(`local:<login>`, created by `create_admin`) and an OAuth sign-in (the IdP
`sub`) are two users even when they belong to one person. Contexts created
through the CLI admin's API key (MCP clients) then read as another creator in
the browser — the **Created by me** filter is empty and the private ones are
hidden — because `created_by` is compared with the session's `user_id`.

When the person keeps using both accounts, link them:

1. Sign in to the web UI with the password account (the CLI admin). A
   password sign-in always starts a new browser session, so it comes first.
2. From the account switcher, choose **Add another account** and sign in with
   the OAuth account. The browser session now holds both.
3. Open **Settings** from the user menu, find **Linked accounts**, and link the other
   account — within 10 minutes of the two proofs (see below; for a Google
   account, use **Confirm with Google** there).

That browser session is the proof: each account entered it through its own
sign-in. An account that is not signed in on the session cannot be linked,
and nothing is ever linked by an email match. An account stays in a browser
session for as long as the session lives, so the link also asks for a recent
proof of each account (#1803, #1818): **both** accounts must have proved
their credential on this browser within the last 10 minutes. A link is
symmetric — a stale proof on either side would let whoever holds an old
session give that account's private contexts to another. When one of them
has no recent proof, the page says so. The request answers `403`
(`AUTH-305`) otherwise.

What counts as a proof (#1818):

- **A password sign-in** (with MFA when the account has it). It always
  starts a new browser session, so sign in with the password first, then
  add the other account.
- **A Google sign-in started from "Confirm with Google"** on the Linked
  accounts card (`GET /api/v1/auth/google/login?add_account=1&link_proof=1`).
  It asks Google for the `auth_time` claim — when the person last actually
  signed in to Google — and records that time, read from the ID token after
  verifying it (Google's signature, `aud` = this deployment's client id,
  `iss`, `exp`, and the same `sub` as userinfo). An `auth_time` older than
  10 minutes does not count, even though the round trip itself just
  happened: a browser with a live Google session goes through without a
  password. Google cannot be asked to re-authenticate (it has no
  `prompt=login` and does not honour `max_age`), so the person signs out of
  Google and in again when their Google sign-in is too old. A missing
  `auth_time` does not count either (fail closed).
- **Not** an ordinary OAuth sign-in or **Add another account**, and **never
  a GitHub sign-in**: GitHub reports no authentication time.
- **Not a sign-in through a provider attached to the account less than
  10 minutes ago** (#1875). Attaching a sign-in provider needs only a live
  session, so a proof through it would be a proof by that session. A new
  account's first sign-in is not affected (the provider was not attached to
  it afterwards); the page says to retry once the 10 minutes have passed,
  and the backend logs
  `link_proof_provider_recently_attached`. This holds with
  `IDENTITY_LINK_ALLOW_OAUTH_SIGNIN_PROOF=true` too.

Google returns `auth_time` only to an OAuth app that is **published (In
production) and Verified**, with **Session age claims** turned on (Google
Auth Platform → Settings → Advanced settings). Without it, a Google account
cannot be linked on this deployment, and the backend logs
`link_proof_auth_time_missing` for each attempt. Ordinary sign-ins send
Google exactly the request they always did.

`IDENTITY_LINK_ALLOW_OAUTH_SIGNIN_PROOF=true` (default `false`) brings back
the #1803 rule for any OAuth sign-in with no provider time: a GitHub sign-in,
an ordinary Google sign-in, or a Google app without Session age claims
counts as a proof at the moment it completed. That proves only that the
browser could complete a sign-in, not that a password was typed — on a
shared or unattended computer someone could add both accounts and link them
that way. Turn it on only where that is acceptable, and sign out of the
provider as well as of Kagura on shared computers. With it on, every OAuth
sign-in counts at the moment it completed, "Confirm with Google" included.

An account keeps its newest proof: a later sign-in that proves nothing, or an
older Google `auth_time`, does not undo it. Sessions started before these
checks existed carry no proof and need a fresh one.

What a link does:

- A private context is open to every account linked to its creator, and the
  memories any of them wrote in it are visible to all of them — in the
  context list, that context's memory list, recall, stats, tags and export.
- The web UI shows those contexts as the viewer's own (`GET /api/v1/auth/me`
  returns `linked_user_ids`).

What it does not do:

- **Roles and membership stay per account.** A link never makes an account a
  system admin, and never lets it reach a workspace it is not a member of.
  The caller is checked as itself: a workspace viewer reads the linked
  account's private context and cannot write to it or change its memories, a
  member needs the context in its `allowed_context_ids`, and a
  workspace-scoped API key cannot open a context outside its workspace. The
  memory list and stats with no context stay the caller's own.
- **Rows keep their author.** `created_by` and `memories.user_id` are not
  rewritten. After an unlink, a memory one account wrote in the other's
  private context is hidden from the context's creator again.
- **Per-account history stays separate**: the graph view and its edges
  (and `explore`), Sleep maintenance (each account's memories are maintained
  on their own, with no de-duplication across the two — merging would delete
  or rewrite one account's rows under the other's name, and an unlink could
  not give them back), the admin memory-health report, the workspace
  dashboard's counts and retrieval feedback. A private context's **access
  patterns** do follow the link (#1807), as its memory list and stats do.
- **Writes that name another memory follow the link only in a private
  context** (#1803). There, an `external_id` upsert from either linked account
  replaces the same row, and `supersedes`, `linked_memory_ids` and
  `linked_source_uris` may point at a memory any of them wrote. The
  replacement is checked as the caller (a workspace viewer cannot replace the
  other account's memory), and an agent binding's memory-type filter does not
  stop it, as it does not for the caller's own row. In a shared context these
  still match the caller's own memories only.
- **Write sessions follow the link in every context** (#2114), as
  `list_revisions` marks `by_you`: `rollback_session` and `changes_since`
  (`session_id`, `by_me`) count a session or window written under a linked
  account as the caller's own. The editor role and each memory's permission
  check stay the caller's, and an agent key still reaches only its own
  agent's sessions.
- A share key recalls as the account that issued it, so it also returns what
  a linked account wrote in that account's private context.

Either account can unlink from **Settings**; the other one does not
have to be signed in. At most 4 accounts can be linked together. In a set of
three or more, unlinking takes one other account out and keeps the rest
linked to you; **Leave** (#1807) takes this account out and keeps the others
linked to each other. A link writes an `identity_linked` `audit_logs` row on both accounts and
emails both a security notice; an unlink or a leave writes
`identity_unlinked` and emails a notice on every account it separates —
in a larger set, the other remaining accounts too. A change takes effect at once, in every API process —
tag suggestions included.

A link outlives the browser session it was made in. Anyone who can sign in
to both accounts on one browser can make one, so unlink from **Profile
Settings** if a notice arrives that you did not expect. A password reset signs
the account out everywhere but does not remove its links; the reset notice
says how many accounts are linked to it, so the owner can review them.

Erasing an account, or deleting a user from the admin API, takes it out of
its link set. A private context it created passes to a linked account that
wrote memories there and could own it as a linked account (a workspace owner
or admin, or a member whose `allowed_context_ids` names it), so what that
account wrote stays readable. The leaving account's own memories in that
context are deleted. A context no linked account wrote in is handled as for
any erased or deleted account. The `delete_admin` command only removes the
user row and its link; the other accounts' rows record who made the link
(`linked_by`), and that becomes empty when the account is gone (#1807).

The endpoints, for a deployment that scripts it: `GET`/`POST
/api/v1/me/account/identity-links`, `POST
/api/v1/me/account/identity-links/unlink` and `POST
/api/v1/me/account/identity-links/leave`, browser session only (an API key
or OAuth bearer token is refused with `403`).

### Moving ownership instead (Issue #1783)

When one of the two accounts is being retired, move its contexts to the other
with the one-shot command below rather than linking. Do not use it for a
person who keeps both accounts: the account that gave its contexts away loses
them, along with every client that signs in as it.


Run the one-shot command where the API runs (same env: `DATABASE_URL` and the
vector store):

```bash
# inside the API container / venv, from backend/
python -m src.cli.transfer_context_creator --from local:admin --to <user_id> --workspace <uuid>               # plan, writes nothing
python -m src.cli.transfer_context_creator --from local:admin --to <user_id> --workspace <uuid> --apply --yes  # write
```

The report goes to stdout; diagnostics go to stderr at `--log-level` (default
`INFO`; `DEBUG` also prints one vector-store line per memory, which on a large
workspace is megabytes of output).

Find the two `user_id`s with `SELECT user_id, name, email FROM users` (the
browser identity is the `id` returned by `GET /api/v1/auth/me`). `--to` must
be the workspace owner or an `admin` member — a member or viewer could end up
owning a private context they cannot list.

For every live context in the workspace whose `created_by` is `--from`, the
command moves `created_by` **and the memories in it authored by `--from`**:
`memories.user_id` and the `user_id` field on each memory's vector-store
point. A private context shows its owner only the memories whose `user_id`
matches, so without that step the new owner would see the context and none
of its content. It also moves **the non-Hebbian edges `--from` holds in the
context** (`neural_memory_edges.origin != 'hebbian'`: declared links,
`supersedes` / `contradicts`, sleep-discovered edges; Issue #1872), so the
new owner can list, update and delete them — a `supersedes` edge keeps
hiding a memory from default recall whoever holds it. Where `--to` already
has an edge on the same pair of memories, one of the two rows goes, chosen
so that recall does not change: `--to`'s row is replaced by the moved edge
when it is Hebbian, when it is semantic and `--from`'s edge is declared, or
when `--from`'s edge is a `supersedes` / `contradicts` and `--to`'s row has
another edge type (a row `--to` declared by hand is never replaced by a
sleep-discovered edge); otherwise `--to`'s row is kept and `--from`'s duplicate is
dropped (the plan prints both counts). One
`audit_logs` row (`context_creator_transferred`) is written per moved
context with the memory and edge counts, and re-running after `--apply`
changes 0 rows.

Vector-store updates run after the database commit; if any fail the command
exits 1 and lists the memory ids — the memory list is already right, recall
may miss those memories until their payload is repaired. A memory that is
not embedded yet (`embedding_status` other than `success`) has no vector
point to update: it is listed as skipped and does not change the exit code,
because the later embed writes the new `user_id`. (During a vector-store
outage those memories are listed as skipped too; the embedded ones are then
reported as failures and the command exits 1.) Re-run with
`--apply --yes --repair-payloads`: in every context an earlier run moved to
`--to` (found by its audit row) it moves any memory still authored by
`--from` and any non-Hebbian edge `--from` still holds — including the edges
a transfer made before this fix left behind — and re-points the vector
payload of every live memory `--to` owns (idempotent; a plain re-run finds 0
contexts to move). Without `--apply`, `--repair-payloads` prints what the
sweep would do and writes nothing. A context `--to` owned all along is never
touched.

The command is **not fenced** against concurrent writers: a `remember` by the
`--from` identity that was authorized before the flip, or an embedding worker
that loaded the old `user_id`, can land after it. Run it while the `--from`
identity's clients (its API key, MCP sessions) are idle, then run it once more
with `--repair-payloads` to sweep anything that slipped in. Run that final
sweep **before the retired account is deleted or erased**. If only the
`users` row was removed, the sweep still works (it needs the `--from`
`user_id`, not its `users` row) while a plain transfer refuses an unknown
`--from`. After an account erasure it finds nothing: the erasure replaces the
`user_id` on the memories and edges that outlive the account with a
pseudonym, so no row matches `--from` any more.

The command does not move API keys: mint a new key for `--to` if MCP clients
should keep seeing the private contexts afterwards. It also leaves other
`created_by` columns (resources, agents, files, secrets), per-user retrieval
history (Hebbian edge weights, feedback, sleep reports — boosting starts over)
and the two user rows untouched.

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

## Supersede suggestions and k-NN seeding: log events (Issue #2023)

After a memory is embedded, one step reads its nearest neighbours. Two features
use that read: the k-NN seed edges (`KNN_SEED_ENABLED`) and the supersede
suggestions (`SUPERSEDE_SUGGEST_ENABLED`, plus the judge behind
`SUPERSEDE_VERIFY_MODE=llm`; see [`supersede_candidate`](mcp-tools.md)). Each
step writes a structured event to the API log. Some events are at DEBUG, so
they never appear with the default `LOG_LEVEL=info`. If a DEBUG event is
missing from your logs, the state may still have happened. Tally the INFO and
WARNING events below.

A re-embed whose summary and content are unchanged since the row's last
successful embed (a content-chunk backfill, an embedding-model switch, a repair
re-queue) runs neither the detection nor the judge. `embedding_completed`
carries `mechanical_reembed=true` for it, so judge spend during a backfill is
counted with `supersede_verify_judged` / `llm_call_log` as before (#2092).

**Supersede suggestions** (`services/memory_service.py`, `services/edge_service.py`)

| event | level | meaning |
|---|---|---|
| `supersede_suggest_disabled` | INFO | `SUPERSEDE_SUGGEST_ENABLED=false` was seen. The flag is read from the environment only (#2010), so this is logged once per process, on its first ingest, not on every ingest: N API or worker processes give N lines |
| `supersede_candidate_detected` | INFO | a candidate was stored. `verdict` is set when the pair has a verdict, either from a judge call or reused from an earlier verdict on the same unchanged pair (no call); it is null otherwise. Count judge calls with `supersede_verify_judged` or the `llm_call_log` rows, not with detected lines that carry a verdict |
| `supersede_candidate_suppressed_by_dismissal` | INFO | a dismissal tombstone (#1504, or `remember(dedupe="off")`) held, so no candidate was stored. Also logged when a dismissal was committed while the embedding run was searching or judging (the row is read again right before every write) |
| `supersede_suggestion_accepted` | INFO | a `supersedes` edge accepted the stored candidate, and the candidate was cleared |
| `supersede_suggestion_dismissed` | INFO | an edit dismissed the candidate and wrote a tombstone |
| `supersede_dismiss_noop` | INFO | there was nothing to dismiss (`reason=parallel`: the judge had set the pair aside and its verdict still matches both memories' texts, so the pair was not shown; a verdict that no longer matches is dismissed like any shown candidate) |
| `supersede_candidate_skipped_source_edited` | INFO | the memory was edited while this embedding run was searching, judging or clearing its candidate, so this run wrote nothing; the edit's own run writes the column |
| `supersede_candidate_skipped_row_changed` | INFO | the row changed between the embedding run's last read and its write (a dismissal, an accept, an edit, a delete or another run's candidate landed in that gap), so this run stored nothing. The write only applies to the row as it was read |
| `supersede_candidate_cleared_no_neighbour` | INFO | a re-embed found no neighbour within 0.02 of the suggestion threshold (usually an edit moved the memory away from the pair), so the stored candidate was cleared. The margin keeps a pair that hovers at the threshold from being cleared and re-judged on every re-embed. Dismissal tombstones are never cleared |
| `supersede_dismiss_malformed_candidate` / `supersede_accept_check_failed` / `supersede_candidate_resolve_failed` | WARNING | the stored candidate could not be dismissed, accepted or read. The edit, edge or read still succeeds; only the suggestion is skipped |
| `supersede_shadowing_applied` | INFO | recall-side: a recall applied the shadowing of memories superseded by an accepted `supersedes` edge (`shadowed` of `candidates` results were hidden) |
| `supersede_shadowing_failed` | WARNING | recall-side: the shadowing lookup failed, so the recall returned without hiding superseded memories |
| `supersede_fold_applied` | INFO | recall-side (#2123): judge-verified older versions were folded under the newest one (`folded` rows removed, in `groups` groups) |
| `supersede_fold_failed` | WARNING | recall-side: the fold could not be computed, so the recall returned without folding |
| `supersede_candidate_skipped_unjudged_low_similarity` | INFO | with the judge on, a neighbour between `SUPERSEDE_SUGGEST_THRESHOLD_LLM` and 0.85 got no `supersedes` / `parallel` verdict (no provider, the daily cap, a failure, or `unclear`), so nothing was stored (#2123). Many of these during an ingest usually mean the daily cap |
| `supersede_detect_skipped_mechanical_reembed` | DEBUG | the re-embed's summary and content are unchanged since the row's last successful embed (#2092), so no detection, judge, store or clear ran; the stored candidate is left as it is |

**The judge** (`SUPERSEDE_VERIFY_MODE=llm`, `services/supersede_verify.py`, #2016)

| event | level | meaning |
|---|---|---|
| `supersede_verify_judged` | INFO | one judge answer, with `verdict`, `confidence`, `verified` and `tokens` |
| `supersede_verify_skipped_newer_target` | INFO | the neighbour was written after the memory, so the pair was not judged and the candidate is stored unverified |
| `supersede_verify_failed` | WARNING | the provider call failed. The candidate is stored unverified |
| `supersede_verify_unparseable` | WARNING | the answer could not be parsed. The candidate is stored unverified |
| `supersede_verify_no_provider` | WARNING | `SLEEP_LLM_PROVIDER` is unset. Logged once per process |
| `supersede_verify_cap_reached` | WARNING | a workspace hit `SUPERSEDE_VERIFY_MAX_CALLS_PER_DAY`. Logged once per workspace per UTC day |
| `supersede_verify_counter_failed` | WARNING | the daily counter could not be read. The judge is skipped (fail-closed) |
| `supersede_verify_ledger_failed` | WARNING | the `llm_call_log` row for a judge call could not be written. The verdict is still used |
| `supersede_suggest_threshold_llm_invalid` / `supersede_suggest_threshold_llm_clamped` | WARNING | `SUPERSEDE_SUGGEST_THRESHOLD_LLM` was unparseable (the default 0.75 is used) or outside 0.5-0.85 (the nearest bound is used) |

**Detection threshold with the judge on** (#2123). With `SUPERSEDE_VERIFY_MODE=llm` the
detection considers the nearest neighbour down to `SUPERSEDE_SUGGEST_THRESHOLD_LLM`
(default 0.75, clamped to 0.5-0.85, env-only) instead of 0.85, and stores a pair below
0.85 only with a `supersedes` or `parallel` verdict. The default is provisional until it is
measured on reworded updates. Each such pair is a judge call against
`SUPERSEDE_VERIFY_MAX_CALLS_PER_DAY` (default 200 per workspace), so raise the cap before a
bulk ingest: once it is reached, later reworded updates are neither stored nor folded in
recall, and pairs at 0.85 or above are stored unverified (they do not fold either). The
lower threshold spends the cap faster than 0.85 did. `remember(dedupe="check")` and the judge-off mode keep 0.85.

The judge's results do not depend on the log. Since #2019, each judge call that
spent tokens is a `caller='supersede_verify'` row in `llm_call_log`, with the
verdict or outcome in `call_metadata`, so the tally survives log rotation. See
[Where cost is recorded](#where-cost-is-recorded-sleep_reports-vs-llm_call_log)
for the query. A `supersede_verify_ledger_failed` line marks a call that is
missing from that table.

**k-NN seed edges** (`services/memory_service.py`, `neural/calibration.py`)

| event | level | meaning |
|---|---|---|
| `knn_seed_edges_created` | INFO | seed edges were written (`edges_created`, `avg_similarity`) |
| `knn_seed_uncalibrated_embedder` | WARNING | the D3 bootstrap gate is crossed but the embedder has no `knn_seed` calibration row, so seed edges are off and supersede detection still runs. Logged once per process per (model, dimensions) |
| `knn_seed_skip_no_consumer` | INFO once, then DEBUG | seeding is on but has no threshold, and suggestions are off, so the ingest reads no neighbours at all. INFO on the first ingest per (model, dimensions) in each process, DEBUG after that. With `reason=mechanical_reembed` (#2092) it is DEBUG only and does not use up the INFO line: suggestions are on but this re-embed's text is unchanged |
| `knn_seed_skip_no_threshold` | DEBUG | no seed edges on this ingest (seeding is off, or there is no calibrated threshold). Supersede detection still ran |
| `knn_seed_disabled_no_calibration` | DEBUG | the threshold resolver found no calibration row. Below D3 this is the normal state of a new deployment or a small context |
| `knn_seed_skip_already_seeded` / `knn_seed_no_neighbors` | DEBUG | a re-embed of a memory that already has edges, or no neighbour above the threshold |
| `knn_seed_skipped_source_edited` | INFO | the memory was edited after this embedding run wrote its vector and before it committed its seed edges, so the edges were dropped; the edit's own run seeds from the new text |
| `knn_seed_edge_failed` / `knn_seed_bootstrap_trigger_failed` / `knn_seeding_failed` | WARNING | best-effort failures. The memory itself is stored |
| `knn_seeding_rollback_failed` | DEBUG | the rollback after `knn_seeding_failed` failed too. The original error is the WARNING line |
| `knn_seed_reload_after_rollback_failed` | DEBUG | after the rollback that follows `knn_seeding_failed` or `tag_cooccurrence_seeding_failed`, the memory could not be loaded again (typically it was deleted meanwhile). A tag co-occurrence seeding that follows then logs its own best-effort failure |
| `knn_seed_lazy_ttl_enqueue_no_loop` / `knn_seed_lazy_ttl_enqueue_unavailable` | DEBUG | an expired calibration row could not queue its recalibration (no running event loop, or the task module is not available). The expired threshold is still used, and the scheduled recalibration catches up |

`knn_seed_skip_no_threshold` and `knn_seed_disabled_no_calibration` stay at
DEBUG. Once the gate is crossed, `knn_seed_uncalibrated_embedder` already
reports the case that matters, and below D3 the event would fire on every
ingest of a fresh deployment. To see them, set `LOG_LEVEL=debug` for a while,
then set it back.

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

## Qdrant dense vector quantization and on-disk storage — Issue #1990

The `dense` named vector of every `kagura_memories*` collection is stored as
fp32 in RAM by default. On a single-server deployment that makes the number
of memories the first ceiling: a 2560-dimension self-hosted embedding costs
about 10 KB per point before the HNSW graph and the payload indexes.

Two settings change the layout. Both default to the historical behaviour.

| Variable | Values | Effect |
|---|---|---|
| `QDRANT_DENSE_QUANTIZATION` | `none` (default), `int8` | Scalar quantization: a 1-byte-per-dimension copy of the dense vector is kept in RAM and used for the HNSW search. The candidates are rescored against the fp32 originals (`rescore=true`, oversampling 2.0), which keeps the ranking within Qdrant's documented <1% recall loss. |
| `QDRANT_DENSE_ON_DISK` | `false` (default), `true` | Store the fp32 originals as memory-mapped files instead of RAM. Combine it with `int8`: searches then read the int8 copy from RAM and touch the disk only to rescore the top candidates. |
| `QDRANT_HTTP_COMPRESSION` | `false` (default), `true` | Lets the REST client accept compressed Qdrant responses. Off by default: a compressed, chunked response can be held about 40 ms by delayed ACK and Nagle on some response sizes (#2112). Turn it on only for a Qdrant reached over a slow WAN link, where bytes matter more than that stall. |

RAM estimate per collection for the dense vector alone (add the HNSW graph,
roughly `points × 16 × 2 × 4 B` with the default `m = 16`, and the payload
indexes):

- fp32: `points × dimensions × 4 B`
- int8: `points × dimensions × 1 B`, plus the fp32 copy — on disk with
  `QDRANT_DENSE_ON_DISK=true`, otherwise also in RAM

Example: 100,000 memories at 2560 dimensions are 1.0 GiB as fp32 and
256 MiB as int8.

### How it is applied

- A new collection is created with the configured layout.
- Right after startup the API reconciles every existing `kagura_memories*`
  collection in a background task, one collection at a time: it reads the
  collection config and sends one `update_collection` only when the
  quantization or `on_disk` differ (log line `collection_dense_config_updated`
  with the changed keys). A collection that already matches is left alone, so
  a restart with unchanged settings sends nothing. Setting the values back to
  `none` / `false` is applied the same way. With `UVICORN_WORKERS > 1` the
  pass is single-flight across the workers (Postgres advisory lock, the same
  guard the scheduled tasks use): the first worker converts every
  collection, the others skip (`dense_vector_config_skipped_busy`); a worker
  that starts later finds nothing to do. A collection whose quantization was
  set by hand to something other than scalar int8 kept in RAM is skipped with
  `collection_dense_config_skipped`; an int8 config with `always_ram=false`
  is treated as a mismatch and converted.
- A collection that a context creates later (including the target of an
  [embedding model migration](#embedding-model-migration--issue-1525)) is
  created with the layout; an existing collection is only ever converted by
  the startup pass, never on a request path.
- The LanceDB backend (`KAGURA_VECTOR_BACKEND=lance`) ignores both settings
  and logs `dense_vector_config_ignored` once when they are not default.
- Qdrant ignores the rescore block on an unquantized collection, so the
  search path is the same in both layouts.

### Turning it on for an existing deployment

The setting is cheap; the conversion is not. After `update_collection` the
Qdrant optimizer rebuilds every segment of the collection in the background.
Searches keep working on the old segments, but until the swap the old and
the new segments coexist, so **disk and RAM grow temporarily** and CPU and IO
are busy. Make the first switch while the host still has free RAM, in a
maintenance window, and watch it finish:

```bash
# 1. Set in the environment and restart the API
QDRANT_DENSE_QUANTIZATION=int8
QDRANT_DENSE_ON_DISK=true

# 2. One log line per converted collection
docker compose logs api | grep collection_dense_config_updated

# 3. Wait until every collection is green again (the optimizer is done)
cd backend && uv run python scripts/check_qdrant_collections.py
#   kagura_memories  status=green  points=...  indexed=...  dense: dims=2560 on_disk=True  quantization=int8
```

Rolling back: set the values back (or remove them) and restart. The
reconciler sends `Disabled` / `on_disk=false` and the optimizer rebuilds once
more. Nothing is re-embedded or re-created in either direction.

`backend/scripts/bench_dense_quantization.py` measures the effect on a
throwaway collection of random vectors (overlap of the fp32 top-k with and
without rescore, p50 / p95), independent of the embedding model. The
search-quality benchmark with real queries
(`docs/search-quality-benchmark.md`) is the check to run on a deployment
before the switch goes to production.

Not covered: binary quantization, HNSW `m` / `ef_construct`, and the LanceDB
backend. Production values and the rollout belong to the operations
repository.

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
# 1. Install the optional backend extra from the lock (adds lancedb + pyarrow)
cd backend && uv sync --locked --extra lite

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

## Orphaned vectors — Issue #1798

Postgres is the system of record; the vector store follows it. Every write
commits the row first and touches the vector afterwards, best-effort. A point
whose row is gone is never returned — recall hydrates hits from live rows — but
it takes a candidate slot and makes the point count useless as an integrity
check.

Two things keep the two stores in step:

- **Deleting a context removes its points**, from every `kagura_memories*`
  collection, right after the soft-delete commits (`merge_contexts` with
  `delete_source` included). Until v0.88.0 the points were kept, and nothing
  removed them later. A vector-store outage does not fail the delete; it logs
  `context_points_delete_failed` and the sweep below picks the points up.
- **An orphan sweep** runs daily at 04:30 UTC. It deletes a memory point whose
  row is gone or was soft-deleted more than an hour ago, and a resource point
  whose context is. It never deletes a point whose memory row is live, and it
  keeps every resource point of a live context — with two exceptions, both
  about a live resource-document memory, whose own point is the resource
  point: a summary point under the memory's id, and a content chunk point of
  the memory. Sleep Maintenance wrote such "label" points before v0.111.0
  ([#2065](https://github.com/kagura-ai/memory-cloud/issues/2065)); chunk
  points of such a memory are ones written before the resource indexer took
  the row over, holding text it no longer has
  ([#2101](https://github.com/kagura-ai/memory-cloud/issues/2101)). Set
  `ORPHAN_VECTOR_SWEEP_ENABLED=false` to turn the scheduled run off.

The scheduled run refuses to delete more than half of the points it scanned
and logs `orphan_vector_sweep_refused` instead: that is what a sweep pointed
at the wrong database looks like. A deployment that deleted large contexts
before v0.88.0 can be in that state legitimately, so **run the command once
after upgrading**, where the API runs (e.g. inside the API container):

```bash
python -m src.cli.sweep_orphan_vectors                 # plan: counts per collection, deletes nothing
python -m src.cli.sweep_orphan_vectors --apply --yes   # delete the orphans
```

The plan lists each collection with its point count and the orphans by reason
(no row, soft-deleted, context gone, document gone, label, chunk), next to the number of live embedded
memories — if the orphans are most of the store, check that `DATABASE_URL`
and `QDRANT_URL` belong to the same deployment before applying. `--apply`
scans again and looks every orphan up a second time before deleting it, then
prints the points left next to the live embedded memories. The two match on a
deployment without resource points, unless an embedding migration still holds a
context's points in its source collection (`migrate_context_embedding
--purge-source` removes those). A second
run reports 0 orphans. `--grace-hours` changes the one-hour grace period.

The sweep assumes this deployment's Postgres owns every point in the
`kagura_memories*` collections. If a second deployment (staging, an evaluation
stack) writes to the same Qdrant under the same collection names, its points
have no row here and would be deleted: set `ORPHAN_VECTOR_SWEEP_ENABLED=false`
on both and do not run the command.

A merge or a Sleep rollback that is still writing points makes the delete pass
wait, up to 30 seconds before each batch; past that the pass stops, keeps what
it has already deleted, and the next run takes the rest. The command then
prints what it deleted (if anything) and what is left, then the error, and
exits non-zero. Only one API
process per deployment runs the scheduled sweep.

The sweep reads Postgres one page of points at a time and ends its
transaction after each page, so a long scan is not cut short by
`idle_in_transaction_session_timeout`.

### Files left in contexts deleted before v0.101 — Issue #1966

Until #1960 (v0.101.0) deleting a context left its files live: hidden from
every listing and from the storage page, but still counted by the upload
quota (`workspace_storage_usage` and the Redis counter). The capacity lock and
the downgrade eligibility (#1961) never counted them, so such a workspace could
downgrade to Free and then have every upload refused with no lock to explain
it. Since #1966 the Redis counter reseeds from the same definition the lock
uses (committed files in live contexts or workspace-scoped), and the leftover
rows are cleared once with a one-shot command:

```bash
python -m src.cli.backfill_deleted_context_files                # plan: one line per deleted context, changes nothing
python -m src.cli.backfill_deleted_context_files --apply --yes  # contexts inside the file restore window
python -m src.cli.backfill_deleted_context_files --apply --include-past-window   # the rest (permanent, see below)
```

The command runs one context per transaction, rewrites that workspace's Redis
counter from the shared definition right after the commit
(`storage_quota_counter_reseeded`), and logs `deleted_context_files_backfilled`
per context (files, uploaded bytes, released bytes). A second run finds
nothing; a context restored, or deleted again, between the plan and its turn
is skipped. `--workspace <id>` limits a run to one workspace.

The files are stamped with their context's own `deleted_at`. For a context
deleted inside the file restore window (6 days) `restore_context` brings them
back with the context, like any deletion since #1960. For a context deleted
earlier — most of what this command finds — the plan prints `past window`:
those files cannot be restored and the nightly file GC removes their objects
on its next run. **That deletion is permanent**, so those contexts are skipped
unless `--include-past-window` is given. Until then such a context can still
be restored with its files; run the plan, read the warning, and keep the
backups' retention in mind before `--apply --include-past-window`.

### Erasures run before v0.123.0 — Issue #2243

**Run this once after upgrading to v0.123.0** if any account was erased on
v0.113.0 through v0.122.x. An erasure on those versions could leave the erased
member's `details` on memories other members authored: in the revision log
(the prior of the next `details` edit after theirs, #2235, fixed in v0.121.0;
erasures on v0.113.0 through v0.120.x) and as the memory's live value where
they made the newest `details` edit (#2244, fixed in v0.123.0). Erasures
before v0.113.0 had no revision log, and erasures from v0.123.0 on do all of
this as they run. Run `--apply` off-peak: each request holds the erased
member's memories locked while it patches the vector store one memory at a
time. Plan first, then apply:

```bash
# inside the API container / venv, from backend/
python -m src.cli.reprocess_erasures --all                # dry run: one line per erasure with changes, writes nothing
python -m src.cli.reprocess_erasures --all --apply --yes  # write; --request <uuid> narrows the run to one erasure
```

The command reads the completed erasure requests and uses the pseudonym each
one stores; it never derives one (no salt, no plaintext needed). Two kinds of
completed request still store the raw account id, and both are skipped and
listed (the id itself is never printed): one the erasure sweep marked
complete without running it because the user row was already gone (`never
ran`), and one finalized before v0.58.1 (`stores the account id`, which
predates the revision log anyway). A value another erased member wrote is
never put back: where it is the one to go back to, the `details` become empty
instead. For each request, in one transaction:

1. where the erased member wrote the live `details` of a memory someone else
   authored, the last value someone else wrote is put back — a new version
   with one revision row (`writer: erasure`, details unavailable), and the
   memory's vector payload (`location`) is patched before the commit;
2. their `details` are removed from every revision prior that still holds
   them. On memories they authored (scrubbed by the erasure), only revision
   rows that existed when the erasure completed are touched — what other
   members wrote there since stays as it is.

A request that changed gets one `account_erasure_reprocess` audit row (counts
and the request id, under the pseudonyms of its `account_erasure` row).
The dry run writes and locks nothing and makes no vector-store call, and its
report prints request ids and counts, never values. It previews each request
on its own: where two erased members edited the same memory, applying the
first can uncover a restore for the second that the plan did not count — the
apply report and the audit rows list it, and a plan afterwards shows 0. A
second `--apply` changes nothing. The vector store must be reachable: a
payload failure rolls that request back and stops the run with exit code 1;
the requests before it stay done (each is logged to stderr as
`erasure_reprocessed` and has its audit row) and a re-run picks up the rest.
A restored memory whose embedding had not succeeded is re-queued instead of
patched, and the sweep re-embeds it.

Two changes reach erasures that run from v0.123.0 on, too:

- **A session rollback the erased member requested no longer counts as their
  `details` edit.** The rollback put back someone else's value, so the prior
  of the next edit over it stays (earlier versions stripped it). The value of
  a session the rollback undid is attributed to whoever wrote it, the same
  way.
- **A value a member erased earlier is never put back.** Until this command
  has run, a revision prior may still hold such a value (the #2235 leftover).
  When a new erasure would go back to it, the memory's `details` become empty
  instead, and the leftover stays in that prior until the command's run for
  the earlier member strips it. Run the command soon after the upgrade, before
  relying on the revision history of memories that members erased on
  v0.113.0 through v0.120.x had edited.

### Restoring a deleted context — Issue #1804

Deleting a context soft-deletes it and its memories, and removes their points.
The rows stay in Postgres until the tombstone purge
(`CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`, default 30 days, counted from the
deletion), and a context can be restored from them within that window:

```bash
python -m src.cli.restore_context <context-id>                 # plan: what would come back, changes nothing
python -m src.cli.restore_context <context-id> --apply --yes   # restore
python -m src.cli.restore_context <context-id> --name notes-2 --apply   # under another name
```

or, as a system admin, `POST /api/v1/admin/contexts/{context_id}/restore` with
`{"dry_run": false}` (`dry_run` defaults to `true`; `new_name` restores under
another name). The restore is recorded in the audit log (`context_restore`),
under the admin for the endpoint and under `--actor` for the command (default
`cli:<OS user>`).

**What comes back.** The context row, and the memories its deletion
soft-deleted: they are live again and marked `pending`, and the embedding sweep
rebuilds their vectors — about 2,400 memories an hour. Until a memory is
re-embedded, recall does not find it. The context's search settings were never
deleted and still apply.

Documents a resource or a connector ingested come back too, and the sweep
rebuilds each one's vector as the indexer wrote it, not from the memory's
summary (which is only the label `[resource] doc vN`): the stored document is
projected again through the resource's latest schema, its text is embedded, and
the point is written under the document's own point id with its facets and
sortable fields. Recall, `forget` and the next version of the document treat it
as they did before the deletion, and nothing has to be ingested again. A schema
published while the context was deleted applies to the rebuilt vectors, as it
would to a re-index.

**What does not come back.**

- Memories the purge already removed. After the retention window the context
  comes back empty — the dry run shows the count before you apply.
- Memories forgotten before the deletion, and memories Sleep merged or archived.
  They were already deleted when the context was; the dry run lists them as
  "stay deleted".
- The context's neural edges (deleted outright; Sleep rebuilds them where it runs),
  its entries in members' context restrictions (`allowed_context_ids` — grant
  them again), and the resource tokens revoked with it.
- The vector of an ingested document the sweep cannot rebuild from its row: the
  resource has no schema left, or the memory's content was edited into
  something that is not a JSON document. The memory is live but ends `failed`,
  with an `embedding_error` that names the document, instead of being given a
  vector of its label. It is final after the first attempt (retrying cannot
  help until one of the two steps below is taken) and is logged once as
  `embedding_resource_unrebuildable`, a warning, not as
  `embedding_budget_exhausted` (the error for a memory that used up its
  retries; with `reason=claim_expired` its runs kept outliving their claim,
  for example a slow embedder on a very long content, and it ends `failed`
  with `embedding_error` `claim_expired: …` until the admin retry below resets
  it). The dry run and the restore both warn with the
  number of memories whose resource has no schema. Two ways to make such a
  memory searchable again:
  - publish the resource's schema again, then reset the failed memories with
    `POST /api/v1/admin/embedding/retry-failed?context_id=<context-id>` (system
    admin); the sweep rebuilds their vectors from the rows, and nothing has to
    be ingested again. This does not help a memory whose content is no longer
    a JSON document;
  - ingest the document again as a newer version (the same version is refused
    as a duplicate).

  The rebuild reads the row, not the ingest history: a memory whose content
  was edited into another JSON document is rebuilt from what it holds now.

**Refusals.** A context that is not deleted; a context of a deleted workspace
(deleting a workspace is final); a context whose name a live context of the
workspace now has (restore with `--name` / `new_name`); a published context
whose `resource_id` a live context now serves. The restore is an admin action:
it does not count against memory quotas, and the workspace's context cap does
not refuse it (the dry run warns when the workspace goes over). Restoring the
source of a merge (`merge_contexts` with `delete_source`) brings back memories
the target context already holds a copy of.

From v0.90.0 a context and the memories deleted with it share one deletion
time, and the restore takes exactly those. Memories deleted with a context
before v0.90.0 carry their own deletion time, a little earlier than the
context's; for such a context the restore takes those up to 10 minutes before
the context's deletion time, deleted by the same user, so a memory that user
forgot in those 10 minutes comes back too. The rows cannot tell such a
deletion from a v0.90.0 one of a context that had no live memories left, so
the window applies there as well; the dry run warns whenever it does, with
the number of memories it would bring back.

The older endpoint `POST /api/v1/admin/contexts/recover` rebuilds a context
from surviving vector-store points, for a context whose rows are gone. It finds
no points for a context deleted on v0.88.0 or later, and says to use the
restore above when the context's row is still there.

## Capacity-over purge rollout — Issue #1969

A Free workspace whose entitlement comes from the billing service
(`plan_name = free` and `entitlement_source = external_billing`) that stays
over the Free capacity for 30 consecutive days has its memories, contexts and
files deleted. The account and the workspace remain; the default context is
kept, empty, so the workspace is usable on Free. Self-hosted and
admin-managed Free workspaces are never stamped, mailed or purged.

The deletion is a soft delete: memories are tombstoned with
`deleted_by = 'system:capacity-purge'` and hard-deleted by the tombstone purge
after `CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`; files are soft-deleted and
hard-deleted by the file GC after its 7-day retention. Neural edges are
dropped, as on a normal context delete.

The daily sweep (`capacity_purge_sweep`, 04:00 UTC, after the 03:15 UTC file
GC, single-flight across API processes) has three stages. Enable them one
deploy apart, in this order:

1. **Stamp**: always on. Writes `workspaces.capacity_locked_since` the first
   time a workspace is seen over the cap, and clears it once the workspace is
   within the cap or no longer Free and billing-managed, so a cleanup or a
   re-subscribe restarts the 30 days. Run it at least one day in production
   and check that the `capacity_lock_stamped` / `capacity_lock_cleared` events
   look right. One workspace that fails to evaluate is logged as
   `capacity_lock_stamp_failed` and left as it was; the others are still
   stamped or cleared (`failed` in `capacity_purge_sweep_done`).
2. **Notify**: `CAPACITY_PURGE_NOTICE_ENABLED=true`. Mails the owner on days
   0/7/14/23/27/29 of the stamp (`capacity_purge_notice_sent`; one Redis claim
   per workspace, stamp and stage, so a missed day sends one catch-up mail,
   not several). The first mail of a lock, once sent or possibly sent
   (delivery timed out), is recorded in
   `workspaces.capacity_purge_first_notice_at`, and the day-29 mail
   ("1 day left") in `workspaces.capacity_purge_final_notice_at`; the stamp
   stage clears both together with the stamp. The record is committed
   before the mail goes out. If that commit fails, no mail is sent, the
   claim is released and the next run retries
   (`capacity_purge_notice_persist_failed`). A mail that fails outright puts
   both columns back as they were (`capacity_purge_notice_revert_failed` if
   that commit fails too).

   **Stages 2 and 3 need a real mail provider** (`EMAIL_PROVIDER=resend`).
   The default logging stub reports every mail as sent without sending
   anything, so the final notice would be recorded and the purge would follow
   with no one warned. Without a real provider each run logs
   `capacity_purge_no_mail_provider` (warning) and skips both the notices and
   the purge, whatever the two flags say. Stamping still runs.
3. **Purge**: `CAPACITY_PURGE_ENABLED=true`. A workspace is purged by the
   04:00 UTC run on calendar day 30 of its stamp, provided its day-29 mail
   went out on an earlier calendar day and its first mail at least 7 calendar
   days before the run. That is the date shown in the mails
   and, once this flag is on, in the web banner and as
   `capacity_lock.purge_at` on `GET /api/v1/workspace/usage/current` (null
   while the flag is off). Because of the stamp stage, the first purge
   happens no earlier than 30 days after stage 1 ran. At most 5 workspaces
   are purged per run, oldest stamp first; the rest wait for the next run.
   The lock is checked again just before the purge. Before deleting
   anything, the purge commits an `audit_logs` row
   (`action = capacity_purge_started`, `resource = workspace:<id>`) with the
   lock start, the over counts and `purge_started_at`. If that commit fails,
   nothing is deleted (`capacity_purge_intent_failed`) and the next run
   retries. After the purge, a second row (`action = capacity_purge`) records
   the counts and bytes. A `capacity_purge_started` row with no
   `capacity_purge` row for the same workspace means a purge whose result
   was not recorded.

   **No deletion without a real warning.** A workspace on day 30 or later
   whose day-29 mail has not gone out on an earlier calendar day, or whose
   first mail went out less than 7 calendar days ago, is not purged: it is
   logged as `capacity_purge_awaiting_final_notice` and counted as
   `purge_awaiting_notice` in `capacity_purge_sweep_done`. So
   `CAPACITY_PURGE_ENABLED=true` with the notices off deletes nothing; a
   day-29 mail that fails or is sent late (a missed run catches up with it)
   moves the purge to the run after the day it went out; and notices turned
   on late (stage 2 enabled when stamps are already weeks old) give every
   owner at least a week between the first mail and the deletion, even if
   that is later than the date the mails show.

   **Memories outside live contexts go too.** The lock counts every live
   memory of the workspace. After the contexts, the purge tombstones every
   memory still live in the workspace (no context, or under a context deleted
   earlier) in the same 1,000-row chunks. Their vectors are removed by the
   orphan vector sweep, not by the purge. After a completed purge the lock is
   measured again; a workspace still over the cap is logged as
   `capacity_purge_still_over` (warning, with the counts) and is not purged
   again in that run.

Before stage 3, list what the next runs will purge:

```sql
SELECT id, capacity_locked_since, capacity_locked_since::date + 30 AS purge_date,
       capacity_purge_first_notice_at, capacity_purge_final_notice_at
FROM workspaces
WHERE deleted_at IS NULL AND capacity_locked_since IS NOT NULL
ORDER BY capacity_locked_since;
```

A row whose `purge_date` is on or before a run's date (UTC), whose
`capacity_purge_final_notice_at` is on an earlier date and whose
`capacity_purge_first_notice_at` is at least 7 days before that date is purged
on that run, unless it is within capacity by then.

**Re-subscribe or owner cleanup during a purge.** Before each context and
after every 1,000-memory chunk, the purge reads the workspace row again and
checks whether anyone other than the purge deleted a memory or a context of
the workspace since the purge started. The purge stops if the workspace
re-subscribed, was erased, or the owner started cleaning up themselves. A
file deleted on its own does not count, because `file_objects` has no
`deleted_by`. The stamp is cleared, the audit row has `"aborted": true` and
`"aborted_reason"` (`resubscribed`, `erased` or `owner_cleanup`),
`capacity_purge_aborted` is logged with the same reason, and no completion
mail is sent. If the workspace is still over the cap, the next stamp run
starts a new 30 days. Non-default contexts are purged first
and the default context last, so an abort leaves the default context's
memories alone. A context the purge stopped in is not deleted, but the chunks
of its memories that were already committed stay tombstoned.

**Restoring after a purge.** A deleted non-default context can be restored
within the tombstone retention, like any deleted context (see
[Restoring a deleted context](#restoring-a-deleted-context--issue-1804)). The
default context is never deleted, so a context restore cannot bring its
memories back. To recover them within the retention, run this in Postgres:

```sql
UPDATE memories
SET deleted_at = NULL, deleted_by = NULL, embedding_status = 'pending'
WHERE workspace_id = '<workspace-id>'
  AND context_id = '<default-context-id>'
  AND deleted_by = 'system:capacity-purge';
```

The embedding sweep then rebuilds their vectors. Memories tombstoned in a
context the purge stopped in, or before a crash part-way through a context,
have a `deleted_at` other than the context's. A context restore does not bring
them back; use the same `UPDATE` with that context's id.

**Completion mail is best-effort.** It is sent once, after the commit that
clears the stamp and writes the audit row, and is not retried. Each purge logs
`capacity_purge_completed` and then one of `capacity_purge_complete_mail_sent`,
`capacity_purge_complete_mail_failed` or `capacity_purge_complete_send_raised`,
all with the same `workspace_id`. A `capacity_purge_completed` with no
`capacity_purge_complete_mail_sent` for the same workspace means the owner
did not get the mail (`capacity_purge_complete_no_owner_email`: no address on
file). Alert on it and follow up by hand.

Other events: `capacity_purge_audit_failed` (the purge ran but its audit
commit failed; the `capacity_purge_started` row is the only record, the stamp
stays and the next run retries. That run usually finds the workspace within
capacity and only clears the stamp), `capacity_purge_intent_failed` (the
`capacity_purge_started` row could not be committed; nothing deleted, retried
next run),
`capacity_purge_nothing_to_delete` (due, but the purge removed nothing:
stamp cleared, audit row written, no mail) and `capacity_purge_failed` (the
purge raised; the stamp stays and the next run retries). The run summary is
`capacity_purge_sweep_done`.

## Embedding model migration — Issue #1525

`python -m src.cli.migrate_context_embedding` moves a context to another
embedding model without a window in which it has no vectors: it fills a second
collection (`--reembed`), checks it (`--verify`), then flips the routing
(`--switch`). The runbook is
[operations/embedding-model-migration.md](operations/embedding-model-migration.md).

**Resource-ingested memories** (documents a resource or a connector ingested)
are migrated as the indexer wrote them, not from the memory's summary, which
is only the label `[resource] doc vN`: the stored document is projected again
through the resource's latest schema, its text is embedded with the target
model, and the point is written under the document's own point id with its
facets and sortable fields. `--verify` looks for that point id and keeps it.
After the switch, recall, `forget` and the next version of the document treat
those memories as before, and nothing has to be ingested again.

A document that cannot be rebuilt from its row — the resource has no schema
left, or the memory's content was edited into something that is not a JSON
document — is skipped: it gets no vector in the new collection rather than a
vector of its label. `--reembed` and `--verify` list those memories on an
`UNREBUILDABLE` line; they do not fail the verification or stop the switch.
Ingest the document again as a newer version (the same version is refused as a
duplicate) to make it searchable.

A context migrated before v0.96.0 has label-only vectors for its
resource-ingested memories; ingesting the documents again as newer versions
repairs them.

**Content chunk vectors** ([#2015](https://github.com/kagura-ai/memory-cloud/issues/2015)).
A context with `content_chunks_enabled` on is migrated with its chunk points:
`--reembed` rebuilds them in the target collection with the target model (up
to 16 extra embedding inputs per memory of 1,500 characters or more; `--plan`
prints the estimate), and a flag turned off while it runs stops the chunk
writes. `--verify` lists memories whose number of chunk points in the target
differs from what their content splits into on a `CHUNKS_MISSING` line. On its
own `--verify` only reports them (the exit code depends on the summary points,
and re-running `--reembed` rebuilds them); when a switch follows (`--run`,
`--verify --switch`), the listed memories' chunks are rebuilt first and the
switch is refused (exit 2) while any remain. `--verify` and `--switch` also delete the chunk
points of memories forgotten meanwhile, and with the flag off `--verify`
removes every chunk point of the context from the target. Turning the flag
off deletes the context's chunk points from every collection, so the source
and the target agree. See [chunking-guide.md](chunking-guide.md).

### Content chunk vectors by default

| Variable | Default | Notes |
| --- | --- | --- |
| `DEFAULT_CONTENT_CHUNKS_ENABLED` | `false` | `content_chunks_enabled` written to **new** context search configs ([#2015](https://github.com/kagura-ai/memory-cloud/issues/2015)). `true` **sends memory content, not only summaries, to the embedding provider** (and the matched passage to the reranker) for every context created from now on; existing contexts are never changed, and a reset never turns the flag on. Needs the Qdrant vector store: `true` with `KAGURA_VECTOR_BACKEND=lance` refuses to start. Shown as `search_defaults.content_chunks_enabled` in `/system/info`. With `CONTENT_CHUNKS_REQUIRE_PLAN=true` a new context gets the flag on only where turning it on would pass: the workspace's plan has the `content_chunks` feature and is below its `max_content_chunk_contexts` (counted under the cap's lock); otherwise it is created off, and the owner can turn it on later within the cap. A `setup_resource` context is then always created off (its indexer rows are never chunked, so it would only take a slot). |

Turning the flag on for a context queues its existing long memories; the
embedding sweep rebuilds them with their chunk points. Sleep Maintenance
re-embeds a memory's chunks only when its content changed and counts the
chunk tokens in `sleep_reports` with the run's other embeddings; metadata-only
changes copy the new values onto the chunk points without an embedding.

### Content chunk vectors as a plan feature

| Variable | Default | Notes |
| --- | --- | --- |
| `CONTENT_CHUNKS_REQUIRE_PLAN` | `false` | Gate content chunk vectors by plan ([#2103](https://github.com/kagura-ai/memory-cloud/issues/2103)). `false` keeps every workspace able to turn `content_chunks_enabled` on in any number of contexts (the behaviour before it existed). `true`: turning the flag on needs a plan with the `content_chunks` feature (M and above, `403` `FEAT-001`) and a free slot under the plan's `max_content_chunk_contexts` (S 0 / M 1 / L 5 / XL 20, `429` `QUOTA-001`, `quota_type: content_chunk_contexts`); turning it off is always allowed. Shown as `features.content_chunks_require_plan` in `/system/info`. |

With the gate on, a context whose workspace plan lacks the feature, or that
ranks past the cap, keeps its stored flag and its chunk points but is treated
as off: recall reads no chunk points for it, and writes, the Sleep rewrite,
the backfill and the embedding migration's chunk rebuild write none. The
contexts that stay active are the earliest-enabled ones
(`context_search_configs.content_chunks_enabled_at`, then the context id), so
the choice over the cap is deterministic. A downgrade purges nothing; an
upgrade makes the kept points active again (and re-queues what is missing,
below). The admin plan
change (`PUT /api/v1/admin/plans/workspaces/{id}/plan`) lists the contexts
that go inactive (`content_chunks_deactivated`, also logged as
`content_chunks_deactivated_by_plan`). The search-config response says why a
context's toggle is disabled (`content_chunks_disabled_reason`). A deleted
context that is restored with the flag on keeps it and ranks as enabled at the
restore, last under the cap: past the cap it is inactive (the restore is not
refused), and `content_chunks_in_use` can then read above
`content_chunks_limit` until the owner turns chunks off in some context.

**Re-queue when a context becomes active again**
([#2107](https://github.com/kagura-ai/memory-cloud/issues/2107)). An inactive
context writes no chunks: a memory written then has none, and a memory whose
content was edited then loses its old ones (they would hold the old text). An
embedding model `--switch` serves the context from a collection that may lack
its chunks (none were rebuilt while it was inactive), or hold the chunks of a
text edited between `--reembed` and the switch. Each of these stamps
`context_search_configs.content_chunks_gap_at`: the write, a merge into such a
context, and — with the gate on — every `--switch` of a context with the flag
on, active or not. The stamp is written at most twice a minute per context,
on its own transaction. A scheduler job beside the embedding sweep (every 60 s,
leader only, no setting) reconciles the stamped contexts that are active now,
whatever made them active: an upgrade, another context turning chunks off or
being deleted, a larger cap. Ten minutes after a context's last stamp (so a
long transaction that made the gap, such as a Sleep run, has committed) it
walks the context's memories, 1,000 per tick and up to 5 contexts per tick
(going round all stamped contexts, so ones that stay inactive hold nobody
back), reads the first chunk point of the long ones in the collection the
context is served from, and re-queues to `pending` only the memories with no
chunk point or with chunks of another text; the embedding sweep rebuilds them
at its usual pace. A tick re-queues at most enough to bring the context's
long memories waiting for the sweep to 100 and the deployment's memories
waiting for the sweep to 200, so new memories are not queued behind the
backfill, and skips a context already at its limit. When the walk reaches the end it clears the stamp, unless a new gap
moved it meanwhile (the next pass then starts over). A context that stays
inactive keeps its stamp. Turning the flag off or on clears it (on re-queues
every long memory anyway). Logged as `content_chunks_gap_recorded`,
`content_chunks_gap_requeued` and `content_chunks_gap_closed`. With the vector
store down, each tick logs up to 5 `content_chunks_gap_reconcile_failed`
warnings (one per picked context, with `context_id` and `error_class`) and one
`content_chunks_gap_reconcile_done` with `failed=5` (fewer when fewer stamped
contexts are active or some wait on their backlog, counted in `deferred`); the
stamps are kept and the next tick retries. The same `error_class` every minute
across several contexts means the vector store is unreachable; one `context_id`
failing on every tick points at that context (its collection, for example). A stamp that could
not be written logs `content_chunks_gap_stamp_failed` (warning), a failed seed
`content_chunks_gap_seed_failed` (error, every tick until it succeeds) and a
tick that fails as a whole `content_chunks_gap_reconcile_tick_failed` (error).
Until a memory is re-queued, its summary point still finds it.

Gaps made before this version were never recorded. With the gate on, the
scheduler leader stamps every context with the flag on once, on the
reconciler's first tick after it starts, retried every tick until it succeeds
(`content_chunks_gap_seeded` in the log; a marker row,
`marker:content_chunks_gap_seed` in `config_overrides`, keeps later starts
from stamping again), so each gets one pass once active: Qdrant point reads,
and only memories without current chunks are re-queued. With the gate off
nothing is stamped and nothing is recorded; turning the gate on later stamps
at the next start.

**Before turning the gate on** on a deployment where contexts already have the
flag on, list the contexts it would make inactive (no write; plan names and
caps are the code defaults — adjust them to any `PLAN_<KEY>_FEATURES` override):

```sql
WITH on_ctx AS (
  SELECT c.id, c.workspace_id, w.plan_name,
         row_number() OVER (
           PARTITION BY c.workspace_id
           ORDER BY coalesce(s.content_chunks_enabled_at, 'infinity'), c.id
         ) AS rn
  FROM context_search_configs s
  JOIN contexts c ON c.id = s.context_id
  JOIN workspaces w ON w.id = c.workspace_id
  WHERE s.content_chunks_enabled AND c.deleted_at IS NULL
)
SELECT * FROM on_ctx
WHERE coalesce(plan_name, 'free') NOT IN ('basic', 'pro', 'promax')
   OR rn > CASE plan_name WHEN 'basic' THEN 1 WHEN 'pro' THEN 5 ELSE 20 END;
```

### Recall LLM selector: opt-in, plan gate and bounds (#2144)

`llm_selector_enabled` (per context, off by default; `update_search_config` or
`PUT /api/v1/contexts/{id}/search-config`) opts a context in to the recall LLM
selector: an MCP recall may send the page's summaries and attached content to
the deployment's judge model (the `SLEEP_LLM_PROVIDER` / `SLEEP_LLM_MODEL` lane
that `SUPERSEDE_VERIFY_MODE=llm` uses), which keeps the results that answer the
query and cuts their content to verbatim passages. Changing the flag takes the
context owner or a workspace owner/admin. Only the MCP `recall` tool runs the
stage; REST `/api/v1/memory/recall`, share keys, agent bootstrap and
`forget(query=...)` never do.

| Variable | Default | Notes |
| --- | --- | --- |
| `LLM_SELECTOR_REQUIRE_PLAN` | `false` | Gate the selector by plan, on the [content chunk](#content-chunk-vectors-as-a-plan-feature) shape. `false` keeps every workspace able to turn `llm_selector_enabled` on in any number of contexts, with no plan query. `true`: turning the flag on needs a plan with the `llm_selector` feature (M and above, `403` `FEAT-001`) and a free slot under the plan's `max_llm_selector_contexts` (S 0 / M 1 / L 5 / XL 20, `429` `QUOTA-001`, `quota_type: llm_selector_contexts`); turning it off is always allowed. Shown as `features.llm_selector_require_plan` in `/system/info`. |
| `PLAN_<KEY>_MAX_LLM_SELECTOR_CONTEXTS` | tier value | Per-tier override of `max_llm_selector_contexts` (`PLAN_FREE_…`, `PLAN_BASIC_…`, `PLAN_PRO_…`, `PLAN_PROMAX_…`). Read only where `LLM_SELECTOR_REQUIRE_PLAN` is on; 0 on a tier without the `llm_selector` feature whatever the number. |
| `LLM_SELECTOR_TIMEOUT_SECONDS` | `8.0` | Time limit of one selector call; past it the recall returns its unselected page. |
| `LLM_SELECTOR_MAX_ROWS` | `10` | Rows of a recall page sent to the selector; rows after them are returned unchanged. |
| `LLM_SELECTOR_MAX_PROMPT_CHARS` | `24000` | Characters of row headers, summaries and content one call may send; a larger page is returned unselected. |
| `LLM_SELECTOR_MAX_CALLS_PER_DAY` | `2000` | Selector calls per workspace per UTC day, a counter separate from `SUPERSEDE_VERIFY_MAX_CALLS_PER_DAY` (recalls must not starve the judge); past it recalls return their unselected page. |

With the gate on, a context whose workspace plan lacks the feature, or that
ranks past the cap, keeps its stored flag but is treated as off. The contexts
that stay active are the earliest-enabled ones
(`context_search_configs.llm_selector_enabled_at`, then the context id). The
admin plan change lists the contexts that go inactive
(`llm_selector_deactivated`, also logged as `llm_selector_deactivated_by_plan`);
an upgrade makes them active again. The search-config response reports
`llm_selector_effective`, `llm_selector_limit`, `llm_selector_in_use`,
`llm_selector_disabled_reason` and `llm_selector_can_change`; `get_context_info`
reports `search_config.llm_selector_enabled` and `llm_selector_effective`.
Migration `f06_2144_llm_selector` adds both columns (`BOOLEAN NOT NULL DEFAULT
false`, metadata-only, and a nullable timestamp); no row is turned on.

**At recall time.** The stage runs after the page is built (fold, rerank and the
`full` content rule have run) on a single-context MCP recall whose flag is
effective, with at least one result carrying content, not under
`include_superseded=true` and not on a degraded search. The model receives the
query, each sent row in rank order with the UTC day it was last written
(`updated_at`, else `created_at`), its summary and its attached content split
into numbered segments — no memory ids, scores, tags or sources; every string
passes the control-token neutraliser and sits between the untrusted-content
markers — and answers with row and segment indices only. The selector judges
whether a row helps answer the query, treating the store as true (#2171): a
row is never dropped or cut because its stored fact contradicts the model's
general knowledge. When two rows answer the same question differently it
prefers the newer one, keeping both when the days match or it is unsure, and
for a counting or listing query it keeps every row that is one of the things
counted. The server validates each answer
item on its own, drops the rows marked irrelevant (their ids are listed in
`selector.dropped_memory_ids`, at most 20) and rebuilds a kept row's content by
slicing the stored text at the chosen segments, joined with `" … "`, with
`spans: [{start, end}]` in stored-content offsets. Nothing the model writes is
copied into a result. With a defaulted `full`, only trusted, non-connector rows
carry content, so only those are sent with content. One provider attempt is
made (no retry) at temperature 0, on the judge's credential lane, paid by the
context's workspace (#708); the recall's own connection is released before the
round trip. Rows the selector drops do not feed Hebbian learning, and an
emptied page gets no `tag_suggestions`; `confidence` still describes the search
before selection.
A kept row whose stored content goes past the 1,500-character head keeps
`content_truncated` / `content_next_offset`.

Every failure returns the unselected page with `selector: {applied: false,
reason}`: `timeout`, `provider_error` (the call or the judge config read
failed), `invalid_answer` (not JSON, or no usable `rows`), `daily_cap`, `too_large` (over
`LLM_SELECTOR_MAX_PROMPT_CHARS`), `no_provider` (`SLEEP_LLM_PROVIDER` empty),
`counter_unavailable` (the Redis day counter could not be read: no call is
made), or `inactive` (the flag is stored on but the plan gate does not cover the
context, or the plan lookup failed — `llm_selector_status_load_failed` says
which). A recall timed with the stage logs `select_ms` on `recall_completed`.
Each provider call writes one `llm_call_log` row with `caller='recall'`,
`call_type='completion'` and `call_metadata.stage='llm_selector'` (outcome
`applied`, `invalid_answer`, `failed` or `timeout`, plus `rows_sent`, `kept`,
`dropped` and the prompt revision); a timed-out call may still be billed but
its usage never came back, so its row carries zero tokens. The admin and
workspace cost dashboards show these rows as their own *By Source* row
(`llm_selector`, also a `source` filter value of the cost aggregation API); a
timed-out call counts as a call with unknown cost (`—`), not as `$0.00` (see
[Where cost is recorded](#where-cost-is-recorded-sleep_reports-vs-llm_call_log)). The
recall audit row
adds `selector_applied` and `selector_dropped_count` when the stage applied.

| Event | Level | When |
| --- | --- | --- |
| `recall_selector_applied` | INFO | the selector applied: `rows_sent`, `kept`, `dropped`, provider, model, tokens, `select_ms` |
| `recall_selector_failed_open` | WARNING (INFO for `daily_cap`) | the stage failed open: `reason`, `error_type`, provider, model, `rows`, `elapsed_ms`. Over the daily cap every recall logs it, so `daily_cap` is INFO; `recall_selector_cap_reached` carries the warning once a day |
| `recall_selector_inactive` | INFO | the flag is stored on but the plan gate treats it as off |
| `recall_selector_cap_reached` | WARNING | a workspace hit `LLM_SELECTOR_MAX_CALLS_PER_DAY`. Logged once per workspace per UTC day |
| `recall_selector_ledger_failed` | WARNING | the `llm_call_log` row could not be written; the page is unaffected |

The stage is measured before any default changes: enable it per context with
`update_search_config(context_id, llm_selector_enabled=true)` on a deployment
with `SLEEP_LLM_PROVIDER` set and compare the same queries with the flag off and
on (tokens from the `llm_call_log` rows above, `select_ms` from
`recall_completed`, misses from `dropped_memory_ids`).

## MCP recall defaults — Issues #2002, #2140

Two settings change what an MCP `recall` returns when the caller leaves an argument out. An explicit argument always wins, and the REST `POST /api/v1/memory/recall` is not affected.

| Variable | Default | Effect |
|---|---|---|
| `MCP_RECALL_DEFAULT_VERBOSITY` | `compact` | Result shape when the caller omits `verbosity` (`compact` or `full`). |
| `MCP_RECALL_DEFAULT_FULL` | `3` | How many of the top results carry their `content` when the caller omits `full` (`0`-`100`; `0` returns summaries only). |

`MCP_RECALL_DEFAULT_FULL` is still a default: as with the built-in 3, only trusted rows (trusted context, not connector-ingested) get content, and an explicit `full=N` from the caller makes every row eligible. Like a bad verbosity, a value outside `0`-`100`, or one that is not a number (`abc`, `3.5`), stops the backend at startup instead of being clamped; note that Pydantic reads `3.0` as 3 and `1_0` as 10. A high value makes every argument-less recall build content that the `max_chars` budget (default 20,000 characters) then drops again from the lowest-ranked rows, so keep it small. The REST default stays `0`.

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
| `RERANK_PROMPT_FORMAT` | `auto` | How the query and documents are sent to `/v1/rerank`. `qwen3` wraps them in the Qwen3-Reranker prompt template (system judge prompt, `<Instruct>` / `<Query>` on the query, `<Document>` plus the empty think block on each document); `raw` sends text verbatim; `auto` picks `qwen3` when the served model name (`RERANK_MODEL`, or a context's `reranker_model`) contains `qwen3-reranker` (case-insensitive), else `raw` — so if `--served-model-name` is something else, set `qwen3` explicitly. In the `qwen3` format the tokenizer's control tokens (`<\|im_start\|>`, `<\|im_end\|>`, `<think>`, …) in the query and in stored memories are broken up so content cannot close the document slot, each document body is cut to 2000 characters and the query to 1000 characters (recall accepts up to 8000), so the whole prompt — about 3100 tokens for common text, CJK included — fits a 4096-token `--max-model-len` and the template's closing tokens survive server-side truncation. With `raw` — including `raw` plus a server-side `--chat-template` — the text is sent as is: no token neutralising or truncation happens on this side. Set `raw` if the server applies the template itself (vLLM `--chat-template`), or the template is applied twice. A forced `qwen3` also templates contexts that override `reranker_model` with a non-Qwen model on the same endpoint; `auto` decides per model. Without `RERANK_BASE_URL` the same setting picks the format of the Ollama path (see [Ollama path](#ollama-path-no-rerank_base_url)). |
| `RERANK_INSTRUCTION` | — | The `<Instruct>` line of the Qwen3 template (both paths); empty ⇒ the model card default, `Given a web search query, retrieve relevant passages that answer the query`. Ignored for `raw`. |
| `SELF_HOSTED_RERANK_MODEL` | `dengcao/Qwen3-Reranker-8B:Q5_K_M` | Model scored one call per document on `SELF_HOSTED_BASE_URL`, used only when `RERANK_BASE_URL` is unset — see [Ollama path](#ollama-path-no-rerank_base_url). The default is an Ollama-registry id — a pure-vLLM stack must set a model it actually serves, or better, use `RERANK_BASE_URL`. |

The `DEFAULT_*` values apply to contexts created from now on; **existing rows
are never rewritten by a migration** (#1207 decision) — see the recipe below.
Unset, they reproduce the previous behaviour (`false` / `voyage` / `rerank-2`).

`GET /api/v1/system/info` (public) exposes `features.reranking`
(`ENABLE_RERANKING` and, for a `self_hosted` default, an endpoint is configured)
and `search_defaults: {use_rerank, reranker_provider, reranker_model,
content_chunks_enabled}` — names only, never URLs or keys.

### Self-hosted recipe (keyless reranking as standard equipment)

Serve a reranker behind `/v1/rerank` and make it the default for new contexts:

```bash
# e.g. vLLM: vllm serve Qwen/Qwen3-Reranker-0.6B --runner pooling \
#            --hf-overrides '{"architectures": ["Qwen3ForSequenceClassification"], "classifier_from_token": ["no", "yes"], "is_original_qwen3_reranker": true}' \
#            --served-model-name qwen3-reranker-0.6b --port 8002
RERANK_BASE_URL=http://reranker:8002
RERANK_MODEL=qwen3-reranker-0.6b
DEFAULT_RERANKER_PROVIDER=self_hosted
DEFAULT_USE_RERANK=true
```

Qwen3-Reranker is trained on a fixed prompt template; fed raw text it scores
nearly everything as relevant and reranking does worse than none. With the
served name containing `qwen3-reranker`, `RERANK_PROMPT_FORMAT=auto` applies
the template on the client side. If you name the model something else, set
`RERANK_PROMPT_FORMAT=qwen3`.

### Ollama path (no `RERANK_BASE_URL`)

Without `RERANK_BASE_URL`, a `self_hosted` context scores each candidate with
one request to `SELF_HOSTED_BASE_URL` (`SELF_HOSTED_RERANK_MODEL`). This is
slower than a batched `/v1/rerank` call and is meant for small Ollama
installs; **`RERANK_BASE_URL` (vLLM `/v1/rerank`, recipe above) is the
recommended self-hosted reranker.** `RERANK_PROMPT_FORMAT` picks the format
here too (#2033):

- **`qwen3`** (what `auto` picks for a model name containing
  `qwen3-reranker` — the default model is one): the Qwen3-Reranker template is
  sent to Ollama's native `POST /api/generate` with `raw: true` (no Modelfile
  chat template on top), `logprobs: true` and one output token, and the score
  is P(yes) / (P(yes) + P(no)) over the first token's top 20 alternatives —
  the model card's yes/no scoring. **Needs Ollama 0.12.11 or later** (older
  versions ignore `logprobs`, every score comes back unparseable and recall
  fails open). vLLM does not serve `/api/generate`: a 404 there (other than
  Ollama's "model not found") fails the recall open and logs
  `self_hosted_rerank_generate_unsupported` (warning, once per process) —
  with vLLM, set `RERANK_BASE_URL` and use `/v1/rerank`. Token neutralising
  and the 2000-character document and 1000-character query cuts apply as on
  `/v1/rerank`; 2000 is about four times the 500 characters the `raw` prompt
  sends per document, so expect noticeably longer per-document latency on a
  CPU or a small GPU. The prompt is sized for a 4096-token context; the
  request does not set `num_ctx` (a per-request value would make Ollama
  reload the model), so keep the model's context at 4096 tokens or more —
  below that, Ollama truncates the prompt and the scores lose their meaning.
- **`raw`**: the generic "Score the relevance … Score: " prompt on the
  OpenAI-compatible `/v1/completions`, with the number in the reply parsed as
  the score — for general instruction models only. **Not supported for
  Qwen3 rerankers**: measured on Ollama, `dengcao/Qwen3-Reranker-8B:Q5_K_M`
  answers `0` for almost every real memory and
  `dengcao/Qwen3-Reranker-0.6B:Q8_0` answers `!!!!!`. Control tokens in the
  query and memories are neutralised here as well; each document is cut to
  500 characters and the query to 1000 characters.

As on `/v1/rerank`, a forced `RERANK_PROMPT_FORMAT` (`qwen3` or `raw`) applies
to every self-hosted context's model, including a context that overrides
`reranker_model` with a different kind of model; `auto` decides per model.

`dengcao/Qwen3-Reranker-0.6B:Q8_0` is unusable on Ollama in either format:
its first-token distribution is flat (every token at the same logprob), so it
never says yes or no. Use the 8B build on Ollama, or the 0.6B model through
vLLM `/v1/rerank`.

A freshly created context on a plan with `reranking` now reranks with no user
action and no external key; a Free workspace does not
(`reranking_disabled_by_plan_tier` in the log). Then convert the contexts that
already exist (one-shot, idempotent):

```bash
# inside the API container / venv, from backend/
python -m src.cli.apply_rerank_defaults --all                # plan: convert/skip per context, writes nothing
python -m src.cli.apply_rerank_defaults --all --apply --yes  # write; --workspace <uuid> narrows the scope
```

The report goes to stdout; diagnostics go to stderr at `--log-level` (default `INFO`).

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
rerank_failed_open provider=self_hosted error_class=ConnectError status_code=None context_id=... workspace_id=...
```

The event carries the error class and, for an HTTP error, the status code —
never the error text, which for an HTTP client error embeds the request URL
and any credentials in it.

A rerank whose scores carry no ranking is checked in one place for every
provider (Voyage, Cohere, `/v1/rerank`, the Ollama path), on the scores of all
candidates before the cut to *k* (#2033):

- **Every candidate scored 0**, or (Ollama path) no reply could be parsed:
  a failure — `rerank_failed_open` with
  `error_class=RerankNotDiscriminatingError`, and the hybrid scores are kept
  instead of being overwritten with 0.
- **Every candidate got the same non-zero score**: not a failure (two
  relevant memories at 1, or duplicate memories, can tie on a small list),
  but no ranking either — the hybrid order and scores are kept and
  `rerank_no_signal` (info, with `doc_count`) is logged.
- A single candidate is never judged, and neither is a provider reply with
  fewer results than candidates (only the returned results are mapped).

On the Ollama path a document whose request failed or whose reply holds no
score is *unscored*. Up to 20% of the candidates may be unscored: they keep
their hybrid order and are placed after the scored ones, reported with a
score scaled under the lowest scored one (the top unscored one ties with it
at most, the ratios of their hybrid scores are kept; [#2084](https://github.com/kagura-ai/memory-cloud/issues/2084)). Above 20% (for example most requests timing out)
the recall fails open with `RerankNotDiscriminatingError`. Either way one
warning per recall gives the counts:

```
self_hosted_rerank_unscored model=... rerank_format=qwen3 unparseable_count=1 failed_count=1 doc_count=10
```

Alert on `rerank_failed_open` (a structured log event — the backend has no
metrics counters). `reranking_disabled_by_deployment` (debug) and
`reranking_disabled_by_plan_tier` (info) mark the two deliberate skips.

### Calibration check

When `ENABLE_RERANKING` is on and either `RERANK_BASE_URL` is set or (without
it) `DEFAULT_RERANKER_PROVIDER=self_hosted` with `DEFAULT_USE_RERANK=true`,
every API process (each Uvicorn
worker) scores one relevant and one irrelevant document once at startup, in
the background (it never runs on a recall), through the reranker recalls use:
`RERANK_MODEL` on `/v1/rerank`, else `SELF_HOSTED_RERANK_MODEL` on the Ollama
path. It covers only that ops-level model, not a per-context `reranker_model`
override. `SELF_HOSTED_BASE_URL` alone does not start the Ollama check — it
also serves self-hosted embeddings — and neither does a self_hosted default
with reranking off by default: either way the check would make Ollama load a
reranker nobody uses. (`DEFAULT_USE_RERANK` does not gate the `/v1/rerank`
check.) A document that fails with a connect error, timeout or HTTP 5xx
retries the check even when the other document scored.

On the Ollama path the scores are always between 0 and 1, so both formats get
the 0.5 verdict below; an unparseable reply counts as 0 and is reported as
`unparseable_count` on `rerank_model_uncalibrated`. The toy pair is easy: a
pass does not prove the `raw` score prompt works on real memories (the 8B
model passes it and still answers `0` for most memories) — the per-recall
check in [Fail-open behaviour](#fail-open-behaviour) covers that.

With the `qwen3` format (and on the Ollama path, either format), the relevant
document must win by at least 0.5, else a warning is logged:

```
rerank_model_uncalibrated model=qwen3-reranker-0.6b prompt_format=qwen3 relevant_score=0.98 irrelevant_score=0.866 margin=0.114 min_margin=0.5
```

That usually means the template is applied twice (the server also runs
`--chat-template`: set `RERANK_PROMPT_FORMAT=raw`) or the model is not a Qwen3
reranker. A passing check logs `rerank_model_calibrated` (info).

With `raw` on `/v1/rerank` the score scale depends on the server
(probabilities, logits), so no verdict is given: the scores are logged as `rerank_calibration_observed`
(info). A Qwen3 reranker served under a name without `qwen3-reranker` shows up
here with close scores — set `RERANK_PROMPT_FORMAT=qwen3`.

Connect errors, timeouts and HTTP 5xx are retried twice (after 10 s, then
30 s) while the reranker loads; an HTTP 4xx, a malformed or incomplete
response (fewer than two scores) or the last failed retry logs
`rerank_calibration_check_failed` (warning, with the error class, the HTTP
status and the endpoint with any password masked) and the check is not
repeated until the next restart.

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
| Content chunk vectors (`content_chunks`; contexts: `max_content_chunk_contexts` 0 / 1 / 5 / 20), only where `CONTENT_CHUNKS_REQUIRE_PLAN` is on | – | ✓ | ✓ | ✓ |
| Recall LLM selector (`llm_selector`; contexts: `max_llm_selector_contexts` 0 / 1 / 5 / 20), only where `LLM_SELECTOR_REQUIRE_PLAN` is on | – | ✓ | ✓ | ✓ |
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

**Back on S: suspended, not deleted** ([#1939](https://github.com/kagura-ai/memory-cloud/issues/1939)).
A workspace that returns to S keeps every object it made on a paid tier, but
the paid-only work those objects drive is suspended while the matching
effective limit is `0`. Nothing is stored: each gate re-reads the limit when it
runs, so re-subscribing resumes everything with no further action.

| Limit at `0` | Suspended | Kept |
|---|---|---|
| `max_connectors` | connector ingest (`FEAT-001`), worker dispatch (`GET /workers/config` answers the not-ready 404, so the worker skips the team), indexing, and the connector's own write key (the workspace API key named `connector:{id}`) — it fails verification (`401`) like a revoked key while the seats are `0` ([#1944](https://github.com/kagura-ai/memory-cloud/issues/1944)), so a worker still holding it cannot `remember` around the pause (a `401` together with the API log line `api_key_rejected_connectors_suspended` is this pause, not a revoked key; no re-issue is needed) | the connector, its token and its key |
| `max_resource_tokens` | ingest on ordinary resource tokens (REST and MCP `ingest_events`, `FEAT-001`), indexing | the tokens and resources |
| `sleep_enabled_contexts_limit` | Sleep's LLM phases (nightly and admin-triggered); a `full` context still runs the non-LLM retention purges | each context's `sleep_mode` |
| `public_calls_per_day` | anonymous public `search` / `info` answer `429` (`api_public_daily`), as bound keys and signed-in callers already did | `is_public` and bound keys |
| `max_members_per_workspace` below the member count ([#1943](https://github.com/kagura-ai/memory-cloud/issues/1943)) | the members without a seat are refused (`403` `FEAT-001`, `feature: members`) after membership and role — seats go to the owner first, then the earliest joined; a non-member keeps its usual refusal. A member without a seat is refused on their own private context too ([#1957](https://github.com/kagura-ai/memory-cloud/issues/1957)) | every membership row; the owner can remove members or upgrade, and the members list marks `seat_paused` |
| `max_contexts_per_workspace` below the live context count ([#1943](https://github.com/kagura-ai/memory-cloud/issues/1943)) | content writes into the contexts beyond the oldest N (`remember`, updates, agent state, edges, uploads, feedback) are refused (`403` `FEAT-001`, `feature: contexts`); reads, listing, export, delete and metadata updates keep working | every context; the contexts list marks `writes_paused`, and deleting an older context frees its slot — and, since [#1960](https://github.com/kagura-ai/memory-cloud/issues/1960), its files and their storage (restorable with the context for 6 days, one day short of the file GC); `DELETE /api/v1/contexts/{id}/files` clears a context's files without deleting it |

Pending resource events are never dropped: a held-back batch stays queued and
is retried hourly. Resource ingest also honours the workspace `memory_limit`
for new documents, counted against the workspace's own live memories: a batch
applies its longest prefix, in event order, that stays within the limit (the
deletes in that prefix free room for the documents after them), and the rest
waits; a batch whose very first new document does not fit is held back. A
batch of only updates and deletes is never held back. `M` keeps
`sleep_enabled_contexts_limit` and `public_calls_per_day` at `0`, so Sleep and
anonymous public serving are suspended there too — except that a tier whose
`PLAN_<KEY>_FEATURES` includes `public_contexts` keeps serving its public
contexts anonymously (`public_calls_per_day` itself has no override).
The same evaluation applies between paid tiers (an XL → L drop seats the first 10 members and
keeps the oldest 20 contexts writable), and to an admin-managed Free workspace with several
members.

**Before upgrading a self-hosted deployment** whose Free workspaces hold more than one member
or context (a team on the default Free workspace, say): set `PLAN_FREE_MAX_MEMBERS_PER_WORKSPACE`
/ `PLAN_FREE_MAX_CONTEXTS_PER_WORKSPACE` to what they hold first. There is no grace period — the
seats and slots take effect on the first request after the upgrade, and every member but the owner
of an over-cap Free workspace would answer `403 FEAT-001` until the cap is raised or members are
removed. Nothing is deleted either way. `GET /api/v1/workspaces/{id}/plan` lists what is suspended in `suspended`
(only features the workspace still has something for), and the web UI shows
the owner a banner.

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
PLAN_PRO_MAX_LLM_SELECTOR_CONTEXTS=10 # with LLM_SELECTOR_REQUIRE_PLAN=true (#2144)
```

### Migration `e95_1951_memories_ws_not_null` (#1951)

`memories.workspace_id` is `NOT NULL` from this migration on, so every memory
counts toward its workspace's limits (a row written before migration 063 could
still carry a NULL and escape them). The migration fills the NULL from the
memory's context and never rewrites a value that is already set. A row with
neither a workspace nor a context cannot be placed: one that is already
soft-deleted is purged (it was only waiting for the tombstone sweep), a live
one makes the migration **refuse to run** with the count and up to ten ids,
schema untouched. Check before upgrading:

```sql
-- live rows the migration cannot place (must be 0 before the upgrade)
SELECT id, user_id, created_at FROM memories
WHERE workspace_id IS NULL AND context_id IS NULL AND deleted_at IS NULL;

-- soft-deleted rows with neither: the migration hard-deletes these, whatever
-- CLEANUP_DELETED_MEMORIES_RETENTION_DAYS says — export the ids first if an
-- erasure audit needs them
SELECT id, user_id, deleted_at FROM memories
WHERE workspace_id IS NULL AND context_id IS NULL AND deleted_at IS NOT NULL;

-- rows the migration will fill (sizes the backfill; each is re-queued for
-- embedding so its search point gets the workspace payload)
SELECT count(*) FROM memories WHERE workspace_id IS NULL;

-- audit only: rows whose stored workspace disagrees with their context's.
-- The migration leaves them as they are; decide separately which side is right.
SELECT count(*) FROM memories m JOIN contexts c ON c.id = m.context_id
WHERE m.workspace_id <> c.workspace_id;
```

Such a live row has no context to restore from: give it a `workspace_id` (and
a `context_id`) by hand after checking its `user_id`, or delete it, then run
the migration again. The upgrade is one transaction, so `memories` is locked
from the `ALTER` to the commit — the backfill touches legacy rows only and the
`SET NOT NULL` scan is a single pass over the table. The `ALTER` waits at most
`lock_timeout` (10 s) for a reader to finish; past that the upgrade fails
without changing anything and you rerun it, rather than every query on
`memories` queueing behind the lock request.

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
| `content_chunks` | `conditional` | Turning `content_chunks_enabled` on is refused, and contexts past `max_content_chunk_contexts` are searched and written without chunks, **only** where `CONTENT_CHUNKS_REQUIRE_PLAN` is on; it defaults to off. |
| `llm_selector` | `conditional` | Turning `llm_selector_enabled` on is refused, and contexts past `max_llm_selector_contexts` are treated as off, **only** where `LLM_SELECTOR_REQUIRE_PLAN` is on; it defaults to off. |

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

### Where cost is recorded (`sleep_reports` vs `llm_call_log`)

Cost is recorded in two places, by shape:

- **Run-shaped: `sleep_reports`** (+ `sleep_report_llm_usage`). One row per
  Sleep Maintenance or Memory Analysis run, with per-phase / per-model usage
  aggregated across the run. All of Sleep's judges (edge discovery, dedup,
  consolidation, importance re-evaluation) are counted here, and only here.
- **Event-shaped: `llm_call_log`.** One row per provider call made outside a
  run: recall embeddings (`caller='recall'`, `call_type='embedding'`), the
  recall LLM selector stage (`caller='recall'`, `call_type='completion'`,
  `call_metadata.stage='llm_selector'`, see
  [Recall LLM selector](#recall-llm-selector-opt-in-plan-gate-and-bounds-2144)) and the write-time
  supersede check (`caller='supersede_verify'`, enabled by
  `SUPERSEDE_VERIFY_MODE=llm`).
  Each row stores its tokens, a cost snapshot priced when it is written,
  `paid_by` (`byok` when a stored workspace key was used, `platform`
  otherwise), and for the supersede check the verdict (`supersedes`,
  `parallel` or `unclear`) and confidence in `call_metadata`. Every call that
  spent tokens is recorded: an answer that could not be parsed has
  `outcome: unparseable`, and a call that failed after the provider had
  answered at least once (two unparseable answers, or one and then an API
  error on the retry) has `outcome: failed` with the tokens of the attempts
  that answered. Only a call that got no answer at all writes no row.

Sleep never writes per-call rows to `llm_call_log`. A run already has a row
that totals its calls, and counting the same spend in two tables would mean
reconciling them on every report. The per-run totals in `sleep_reports` are the
record of Sleep cost.

The admin and workspace cost dashboards combine Sleep, Memory Analysis, the
supersede check and the recall LLM selector, and each one appears as its own
row in the *By Source* table (sources `sleep`, `analysis`, `supersede_verify`,
`llm_selector`). They price the two kinds differently: Sleep and Memory
Analysis usage is priced from `llm_pricing` each time the dashboard is read, so
a price change also changes past Sleep figures, while a supersede-check or
selector row keeps the cost it was given when written. Both skip
`pricing_model = 'subscription'` rows, so a model billed by subscription shows
as cost unknown (`—`) in either (selector rows written before #2154 kept
subscription pricing and keep the cost they were given). A timed-out selector
call (`call_metadata.outcome='timeout'`, zero tokens because its usage never
came back) is counted as a call with unknown cost. Unknown cost is sticky like
a pricing miss: the user's period total, the model row and the selector's *By
Source* total over the selected range show `—` rather than a partial sum.
Recall embeddings are in `llm_call_log` but are not shown in the dashboards
yet. Because the ledger is a table, you can still answer questions such as
"how did the supersede verdicts split last week, and what did they cost?"
after your logs have rotated:

```sql
SELECT coalesce(call_metadata->>'verdict', call_metadata->>'outcome') AS verdict, count(*),
       sum(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS tokens,
       sum(cost_usd) AS cost_usd
FROM llm_call_log
WHERE caller = 'supersede_verify' AND occurred_at >= now() - interval '7 days'
GROUP BY 1;
```

The log events for the same path (`supersede_verify_*`, the k-NN seeding
events and their levels) are listed in
[Supersede suggestions and k-NN seeding: log events](#supersede-suggestions-and-k-nn-seeding-log-events-issue-2023).

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

## Database Connection Pool — Issue #1980

Each API process opens PostgreSQL connections through SQLAlchemy's `QueuePool` (`backend/src/db/base.py`). Three variables size it:

| Variable | Default | Meaning |
|---|---|---|
| `DB_POOL_SIZE` | `5` | Connections the pool keeps open (minimum `1`). |
| `DB_MAX_OVERFLOW` | `10` | Extra connections opened under load and closed again when returned (minimum `0`). |
| `DB_POOL_TIMEOUT` | `30` | Seconds a request waits for a free connection once `DB_POOL_SIZE + DB_MAX_OVERFLOW` are checked out (must be greater than `0`). |
| `DB_NULL_POOL` | `false` | Test processes only: open a fresh connection per checkout (SQLAlchemy `NullPool`) on both engines; the three sizing values are still validated but no engine reads them (#1988). Never set it in a deployment; the API logs `database_null_pool_enabled` when it is on. |

`DB_POOL_SIZE=0` and `DB_MAX_OVERFLOW=-1`, which SQLAlchemy reads as "unlimited", are refused at startup, and so is `DB_POOL_TIMEOUT=0`: the async pool's timed checkout cancels a zero-second wait before it can take an idle connection, so every request would read as `pool_exhausted`. For fail-fast behaviour use a small positive value such as `1`. `DB_POOL_SIZE + DB_MAX_OVERFLOW` must be at least `2`, because the scheduler leader keeps one pooled connection for the process lifetime and requests need another. Keep it at `15` or more: the analysis labeler borrows up to 8 connections at once next to its orchestrator session, and the API logs `database_pool_below_fanout` at startup when the configured pool is smaller than that.

**Sizing.** The sync engine used by the OAuth2 server shares the same three values, so one process can hold up to `2 × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` connections. Across the deployment, `uvicorn workers × 2 × (DB_POOL_SIZE + DB_MAX_OVERFLOW)`, plus the connections the Sleep and background-worker jobs open, plus the operators' `psql` sessions, must stay below PostgreSQL's `max_connections`. Raise `max_connections` first if the sum does not fit.

**When the pool runs out.** A request that cannot get a connection within `DB_POOL_TIMEOUT` gets `503` with error `DB-002`, `Retry-After: 5` and `details.reason: "pool_exhausted"`; `connection_refused` (the database did not accept the connection), `deadlock` (Postgres aborted the transaction to break a lock cycle, SQLSTATE `40P01`; a retry normally succeeds) and `database_error` (any other SQLAlchemy failure) tell the other causes apart. The `database_error` log event carries the same `reason` field. A shorter `DB_POOL_TIMEOUT` (for example `5`) makes exhaustion fail fast as that 503 instead of holding the request for 30 seconds; the default stays `30` until the deployment's values are chosen.

Recall, explore and `reference` record access statistics (`access_count`, `last_used_at`, `accessed_by_clients`, and `reference_count` for `reference`) after the read commits, in a statement that skips rows another transaction holds instead of waiting for them (#1985). Under contention a surfacing can therefore go uncounted; the statistics are a lower bound, and a busy row never turns into a `deadlock` 503. The Hebbian learning pass of a recall is skipped (log `hebbian_update_skipped_busy`) while another pass, a graph batch task or that user's Sleep run holds the same user's graph: Sleep takes the lock for its whole run, so nothing is learned while a user sleeps.

One lane does not carry `details.reason`: MCP tool calls report the same failure as the envelope error `service_unavailable` (`backend/src/mcp_server/tools/_errors.py` classifies the pool timeout with the other dependency failures); the server log still names the exception type. A REST request whose credential lookup itself hits the exhausted pool gets the same `503` `DB-002` with `details.reason` as any other request, never a `401` that would make the client discard its credential and re-authorize: the `kagura_*` API-key lookup always propagated, and the OAuth Bearer lookup does so since #1981 (before that it answered `401`).

### Recall stage timings — Issue #1989

Every recall that completes logs one `recall_completed` line (also when the page is empty; a recall that raises logs nothing, so failed requests are absent from these numbers) with the time each stage took, as integer milliseconds:

| Field | Stage |
|---|---|
| `duration_ms` | From the start of `MemoryService.recall` to the log line (everything the caller waited for inside the service; authentication, the route handler and serialization are outside it). |
| `embed_ms`, `embed_cache_hit` | Query embedding (`SearchService.hybrid_search`); `embed_cache_hit: true` means the vector came from the cache, `false` a provider call (also one that failed). |
| `qdrant_ms`, `fulltext_ms` | The dense and the BM25 search. When a recall is `degraded` (#1515), the failed arm carries its time-to-failure: `embed_ms` for `embedding_unavailable`, `qdrant_ms` for `vector_search_unavailable`; the rest of the recall ran keyword-only. |
| `rerank_ms` | The cross-encoder rerank; absent when there was nothing to score or no active provider (never `0` for a skipped one). A rerank that failed open (`rerank_failed_open`) keeps its time-to-failure here. |
| `hydrate_ms` | The Postgres row fetch. |
| `select_ms` | The recall LLM selector (#2144), including its plan-gate check; absent when the stage did not run. |
| `finalize_ms` | Response build, explore hints, related tags, confidence and the `memory_access_events` row. |
| `commit_ms` | The read transaction's commit, before the post-commit writes. |
| `post_commit_ms` | The access-statistics and Hebbian writes that run after the read commits (#1985) and still delay the response. |
| `other_ms` | `duration_ms` minus the stages above: preparation and config lookups, the `llm_call_log` write after a cold embed, the plan-tier check before a rerank, and the binding, supersede, reinforce and selection passes between hydrate and the response build. |
| `candidates`, `results`, `k`, `context_count`, `degraded` | Size context: candidate pool, returned page, requested page, contexts searched; `degraded: true` when the semantic arm was unavailable (#1515). |

The stages run one after another, so `embed_ms + qdrant_ms + fulltext_ms + rerank_ms + hydrate_ms + select_ms + finalize_ms + commit_ms + post_commit_ms + other_ms` equals `duration_ms` up to rounding. `memory_access_events.latency_ms` and `usage_stats` keep the total only; the breakdown lives in the log. A p50 / p95 per stage from the live color's JSON log (`LOG_COLORIZE=false`, the production default; the colored console renderer is not JSON):

```bash
docker logs kagura-api-blue --since 1h 2>&1 | grep recall_completed | jq -s '
  def pct(p): sort | .[((length - 1) * p | floor)];
  def stage(f): {p50: (map(f // empty) | pct(0.5)), p95: (map(f // empty) | pct(0.95))};
  {duration: stage(.duration_ms), embed: stage(.embed_ms), qdrant: stage(.qdrant_ms),
   fulltext: stage(.fulltext_ms), rerank: stage(.rerank_ms), hydrate: stage(.hydrate_ms),
   select: stage(.select_ms), finalize: stage(.finalize_ms), commit: stage(.commit_ms), post_commit: stage(.post_commit_ms),
   other: stage(.other_ms)}'
```

To see the slow requests with their breakdown beside them: `... | grep recall_completed | jq -r '[.duration_ms, .embed_ms, .qdrant_ms, .fulltext_ms, .hydrate_ms, .finalize_ms, .commit_ms, .post_commit_ms, .candidates] | @tsv' | sort -n | tail`.

## Multiple API workers and Postgres connections — Issue #1980

By default each API container (`api-blue` / `api-green` in the single-server
compose files) runs **one** uvicorn process. `UVICORN_WORKERS` in `.env.prod`
starts that many worker processes per container instead (the compose files pass
`--workers ${UVICORN_WORKERS:-1}`). Unset, nothing changes.

Each worker is a full, independent process:

- **Its own connection pools.** Every worker opens its own async SQLAlchemy
  pool, its own sync pool (OAuth2 only) and its own Redis pool
  (`REDIS_MAX_CONNECTIONS` is per worker, see below). The DB pool size per
  engine is `DB_POOL_SIZE + DB_MAX_OVERFLOW` (Issue #1980).
- **Its own in-process caches.** The LLM price cache (`llm_pricing_service`),
  the tag vocabulary `TTLCache` (`tag_resolution`), the MCP rate-limit
  settings cache (`_RATE_LIMIT_CACHE`, 60 s) and the MCP capacity-gate
  "not a candidate" cache (`_NOT_CANDIDATE_CACHE`, 30 s) are per process.
  The two MCP caches are judgement caches: a workspace that has just hit
  its limit can pass on each worker until that worker's cached entry
  expires, so the admit window after a limit grows with the worker count. They are caches over
  Postgres / Redis, which stay the source of truth, so N copies only lower the
  hit rate; nothing becomes inconsistent.
- **Known limit: legacy MCP sessions are per process.** The session table of
  the legacy MCP era (`backend/src/mcp_server/session.py`, `MCPSessionManager`
  — clients that still open with the `initialize` handshake) and its `DELETE`
  tombstones (#1740) live in each worker's memory. With several workers a
  legacy client's next request can land on another worker, which re-adopts
  its server-minted session id as it would after a restart: the negotiated
  `protocol_version` is unknown there, so the revision `initialize` agreed on
  is lost. A `DELETE` tombstone is only honoured by the worker that received
  the `DELETE`; another worker may re-adopt that id. Clients on the current
  stateless revision (2026-07-28, `mcp_server/transport_stateless`) carry no
  session and are unaffected. This holds for `UVICORN_WORKERS > 1` until
  legacy sessions move to a shared store.
- **The background scheduler runs on one process of the deployment at a
  time.** Every worker runs the app lifespan, but only the one that takes the
  Postgres advisory lock `apscheduler_leader` registers the cron jobs and
  starts APScheduler (log line `scheduler_leader_acquired`, with its `pid`).
  Startup does not wait for this: the first attempt runs in the background
  right after boot. The leader keeps the lock on one connection of its own
  async pool, so that worker's effective DB pool is one connection smaller.
  The others log `scheduler_leader_standby` once and retry every
  `SCHEDULER_LEADER_RETRY_SECONDS` (default `60`, minimum `5`). When the leader
  process exits — a worker restart, or `deploy.sh` stopping the old color after
  a blue/green switch — Postgres drops its lock and a standby in either color
  takes over within that interval; a cron tick that falls in that gap is
  skipped. The leader checks its lock connection every
  `SCHEDULER_LEADER_PING_SECONDS` (default `10`, minimum `1`); when it breaks
  or stops answering (e.g. Postgres restarted) the leader stops its scheduler
  (`scheduler_leader_lost`) and competes again. If Postgres is unreachable at
  startup the worker logs `scheduler_leader_check_failed`, keeps serving
  requests and retries.
  `SCHEDULER_LEADER_ENABLED=false` switches the leader loop off in a process
  (#1988); the test suite sets it so no cron job runs inside pytest. Leave it
  on in every deployment: with it off everywhere, no process runs the jobs.

  The lock serialises schedulers, not job executions, and has two bounded
  overlaps:
  - **Handover:** the old leader's scheduler stops without waiting for running
    jobs, so a job already running on the old leader can overlap the new
    leader's first tick of the same job.
  - **Postgres restart:** after a Postgres restart the old leader keeps
    dispatching for at most `SCHEDULER_LEADER_PING_SECONDS` + 15 s while a
    standby may already hold the lock.

  The Sleep, neural and capacity purge jobs stay `single_flight` guarded
  against both.
- **Every worker still runs its own MCP session cleanup.** It cleans that
  process's session table (the known limit above), so it is a per-worker loop
  every `MCP_SESSION_CLEANUP_INTERVAL` seconds (default `600`), not a
  scheduler job.

### Sizing `POSTGRES_MAX_CONNECTIONS`

Every connection the workers can open must fit under Postgres'
`max_connections`, which the compose files set from `POSTGRES_MAX_CONNECTIONS`
(default `100`, the image's own default). Per color:

```
UVICORN_WORKERS × 2 × (DB_POOL_SIZE + DB_MAX_OVERFLOW)   # async + sync engine per worker
```

The scheduler leader's lock connection is checked out of that worker's own
async pool, so it does not add to this ceiling.

During a blue/green switch both colors run, so double it. Then add every
other client of the same Postgres — co-resident workers, Sleep or CLI runs
started with `docker compose exec` / `run`, an operator's `psql` — and keep
the total below `POSTGRES_MAX_CONNECTIONS` with some headroom. Example with the pool defaults (5 + 10)
and `UVICORN_WORKERS=2`: `2 × 2 × 15 = 60` per color, `120` during a
switch — above the default `100`, so raise `POSTGRES_MAX_CONNECTIONS` first.
Every Postgres connection costs memory on the data host, so prefer a smaller
pool over an enormous `max_connections`.

### How many workers

Start from the CPU, not from the pool: a 4-core host is typically served well
by 2–4 workers. Before adding workers, check under load that the API process
is actually CPU-bound; if it is waiting on a full DB pool (`503 DB-002`,
timeouts while CPU is idle), tune the pool (Issue #1980) first. Each worker
also holds its own copy of the application in memory.

### Rolling it out

1. Set `POSTGRES_MAX_CONNECTIONS` in `.env.prod` and recreate the Postgres
   container — it reads `max_connections` only at start, so this is a short
   database restart. Check with `SHOW max_connections;`.
2. Set `UVICORN_WORKERS` and deploy as usual; confirm exactly one
   `scheduler_leader_acquired` line across both colors.

On a split host the Postgres service is defined in `docker-compose.data.yml`
on the data host. That host does not `git fetch` this repository, so the
`command:` line for `max_connections` has to be carried over to its copy of
the file by hand — follow your ops runbook for changing the data host.

## Redis Connection Pool (rate limits and daily quotas) — Issue #1556

Per-minute rate limits and the daily MCP / REST / Public API quotas above are counters in Redis, incremented by `RateLimitMiddleware` on every authenticated request. The singleton client (`backend/src/db/redis.py`) uses a `BlockingConnectionPool`: when all pooled connections are checked out, a request waits up to `REDIS_POOL_TIMEOUT_SECONDS` (default `2.0`) for one to free up instead of failing immediately. Size the pool with `REDIS_MAX_CONNECTIONS` (default `50`, per API worker process). Quota checks are **fail-open** by design — if Redis is down, or the pool wait times out, the request is allowed and the counter update is lost (or, if Redis fails between the `INCR` and the `EXPIRE` that follows it, only partially applied). Each such miss logs exactly one `quota_check_failed_open` warning with `quota` (`per_minute`, `daily_mcp`, `daily_public`, `daily_rest`), `key_prefix` and `error_class` fields. Alert on that event: a sustained stream of it means quotas are not being enforced and either Redis or the pool size needs attention.

## Redis Password (single-server compose) — Issue #1794

The single-server compose files run Redis without a password by default: it
listens only on the compose network (and, on a split host, on the data VM's
private address through the `data-expose` overlay). Set `REDIS_PASSWORD` in
`.env.prod` and Redis refuses unauthenticated commands; the API's default
`REDIS_URL` is built from the same variable, so on a single host that one line
is all:

```bash
# .env.prod
REDIS_PASSWORD=<output of: openssl rand -hex 32>
```

- **Use a hex password.** It needs no quoting in `.env.prod` and no encoding in
  a URL. Any other value has to be single-quoted in `.env.prod` — unquoted, the
  env-file parser expands `$` and cuts the value at ` #`; double-quoted, it
  still expands `$`. With `/ # ? [ ]` or `%` in it, it also needs an explicit
  `REDIS_URL` (below).
- **`REDIS_URL` in `.env.prod` overrides the built URL**, whole: scheme,
  password, host and port. Set it when Redis is somewhere else, or when the
  password has a character that breaks the built URL — the compose file pastes
  the password in as it is and cannot encode it. `/`, `#`, `?`, `[` and `]`
  make the URL unparseable: the API refuses to start and, being
  `restart: always`, keeps restarting. A `%` followed by two hex digits is
  decoded into another character, so the API sends the wrong password. Other
  characters (`@ : + =` among them) work unencoded. Step 1 of
  [Turning it on](#turning-it-on) catches both cases before Redis is restarted.
  To percent-encode the password without it landing in your shell history:
  `read -rs P && printf '%s' "$P" | python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read(), safe=""))'`,
  then `REDIS_URL='redis://:<encoded>@redis:6379'`.
- **Split host:** the data VM's `.env.prod` needs `REDIS_PASSWORD` (Redis
  enforces it), the app VM's needs `REDIS_PASSWORD` too, or a `REDIS_URL` with
  the data VM's address — the built URL uses `REDIS_HOST`.
- **A password only in `REDIS_URL` protects nothing.** The API authenticates
  against a Redis without a password just as well, so `/readiness` stays green
  while Redis is open. `REDIS_PASSWORD` is what turns auth on; step 3 below is
  the proof.
- **Your shell wins over `.env.prod`.** Compose prefers a `REDIS_PASSWORD` or
  `REDIS_URL` exported in the shell that runs it; `unset` them first.
- **Where it ends up.** Redis gets the password as its `--requirepass` argument
  and as `REDIS_PASSWORD` in its environment, which its healthcheck uses. For
  an interactive client:
  `docker compose -f docker-compose.prod.yml exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" exec redis-cli'`.
  Like the PostgreSQL password, it is visible to anyone who can run
  `docker inspect` on the host; Redis rewrites its process title, so `ps` does
  not show it.
- **Unset or empty** keeps the old behaviour exactly: an empty `requirepass`
  means no auth.

**Upgrading to the release that ships this (v0.87.0).** The Redis service
definition changed, so the next whole-stack `docker compose up -d` — including
the `kagura-memory` unit at boot — recreates Redis once: a restart, data kept.
And `REDIS_URL` / `REDIS_PASSWORD` lines already in `.env.prod`, which the
compose files used to ignore, now apply — check with
`grep -nE '^(REDIS_URL|REDIS_PASSWORD)=' .env.prod` before you deploy and
remove any you did not mean (a `REDIS_URL` copied from the development
`.env.example`, for instance, points the API at its own container).

### Turning it on

Redis restarts with the new configuration and every client has to reconnect
with the password, so do it in a maintenance window. Between the Redis restart
and the API recreate, every request that needs Redis fails: session lookups
(signed-in pages, OAuth) error, and rate limits and quotas fail open. Run the
two commands of step 2 back to back.

```bash
cd /opt/kagura-memory/src/terraform/single-server
# 1. Add REDIS_PASSWORD to .env.prod (above), then check the render without
#    printing the secret. It prints two words and should print "True True":
#    Redis gets a password, and the API's REDIS_URL parses to a host, a port
#    and that same password. Do not go on to step 2 unless it does.
docker compose -f docker-compose.prod.yml --env-file .env.prod config --format json | python3 -c '
import json, sys, urllib.parse
s = json.load(sys.stdin)["services"]
# The render writes a literal $ as $$.
password = s["redis"]["command"][-1].replace("$$", "$")
try:
    u = urllib.parse.urlsplit(s["api-blue"]["environment"]["REDIS_URL"].replace("$$", "$"))
    url_ok = (
        u.scheme in ("redis", "rediss")
        and bool(u.hostname)
        and u.port is not None
        and urllib.parse.unquote(u.password or "") == password
    )
except ValueError:
    url_ok = False
print(password != "", url_ok)'

# 2. Restart Redis with the password, then recreate the running API colors so
#    they reconnect (xargs -r: with no color running, recreate nothing rather
#    than the whole stack; add --no-build on a registry-mode host):
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps redis
docker ps --format '{{.Names}}' | grep -oE 'api-(blue|green)$' \
  | xargs -r docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --no-deps --force-recreate

# 3. Verify — required: Redis healthy, refuses a client without the password,
#    API ready.
docker inspect -f '{{.State.Health.Status}}' kagura-redis      # healthy
docker exec kagura-redis redis-cli ping                        # NOAUTH Authentication required.
./scripts/deploy.sh --status
```

If step 1 prints `False` first, `REDIS_PASSWORD` did not reach Redis (not in
`.env.prod`, or emptied by a variable exported in your shell). If it prints
`False` second, the API would not reach Redis with that password: either the
password has a character that breaks the built URL (`/ # ? [ ]`, or `%`), or
an explicit `REDIS_URL` carries a different password. Switch to a hex password
or set an explicit, percent-encoded `REDIS_URL` (above), then run step 1
again. An API started with a URL that does not parse exits at start-up, and
its log says `REDIS_URL is not a valid URL` without quoting the value.

On a split host, step 2 runs in two places: Redis on the data VM
(`DATA_BIND_ADDR=… docker compose -f docker-compose.data.yml -f docker-compose.data-expose.yml --env-file .env.prod up -d --no-deps redis`),
then the API colors on the app VM with `-f docker-compose.app.yml`.

**What survives the restart.** Redis keeps its data on the `kagura_redis_data`
volume — an append-only file (`--appendonly yes`) plus the snapshot Redis
writes when it stops cleanly, and a compose recreate stops it with SIGTERM.
Sessions (`session:*`) and the embedding spend counters (`embed_spend:*`) are
reloaded with their TTLs: nobody is signed out and no spend is forgotten.
`scripts/tests/compose_redis_auth.bats` checks this against the image the
compose files pin.

To rotate the password, change it and run steps 2–3 again. To turn auth off,
remove it and do the same.
