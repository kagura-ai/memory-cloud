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
keyed HMAC is stored in `user_known_devices`; no IP address is stored there (the
IP and user agent go into the email and, while a notice is coalesced or retried,
into the notice queue in Redis). A daily
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
regenerated, or a provider sign-in changes the account's email address (the
previous address is told), the account owner is
emailed a notice (UTC time, IP address, user agent, key or client name, and the
acting admin for admin actions — never a secret, token or link other than the
plain `FRONTEND_URL/profile` page). The notices cannot be turned off. They go
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

## One person, two accounts — linking them (Issue #1784)

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
3. Open **Profile Settings**, find **Linked accounts**, and link the other account.

That browser session is the proof: each account entered it through its own
sign-in. An account that is not signed in on the session cannot be linked,
and nothing is ever linked by an email match.

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
- **Per-account history stays separate**: the graph view and its edges, Sleep
  maintenance (each account's memories are maintained on their own, with no
  de-duplication across the two), memory health, access patterns, the workspace dashboard's counts and
  retrieval feedback.
- **Writes that name another memory stay per account**: an `external_id`
  upsert replaces only the caller's own earlier memory, and `supersedes` /
  linked memory ids must point at the caller's own memories. Two accounts
  that upsert the same `external_id` into one private context keep two rows.
- A share key recalls as the account that issued it, so it also returns what
  a linked account wrote in that account's private context.

Either account can unlink from **Profile Settings**; the other one does not
have to be signed in. At most 4 accounts can be linked together. Every link
and unlink writes an `audit_logs` row (`identity_linked`,
`identity_unlinked`) on both accounts and emails both a security notice. An
unlink takes effect at once; tag suggestions can keep the other account's tag
names for up to two minutes.

A link outlives the browser session it was made in. Anyone who can use a
browser where both accounts are signed in can make one, so treat a shared
browser as you would for any signed-in session, and unlink from **Profile
Settings** if a notice arrives that you did not expect.

Erasing an account, or deleting a user from the admin API, takes it out of
its link set. A private context it created passes to a linked account that
wrote memories there and could own it as a linked account (a workspace owner
or admin, or a member whose `allowed_context_ids` names it), so what that
account wrote stays readable. The leaving account's own memories in that
context are deleted. A context no linked account wrote in is handled as for
any erased or deleted account. The `delete_admin` command only removes the
user row and its link.

The endpoints, for a deployment that scripts it: `GET`/`POST
/api/v1/me/account/identity-links` and `POST
/api/v1/me/account/identity-links/unlink`, browser session only.

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
of its content. One `audit_logs` row (`context_creator_transferred`) is
written per moved context, and re-running after `--apply` changes 0 rows.
Vector-store updates run after the database commit; if any fail the command
exits 1 and lists the memory ids — the memory list is already right, recall
may miss those memories until their payload is repaired. Re-run with
`--apply --yes --repair-payloads`: in every context an earlier run moved to
`--to` (found by its audit row) it moves any memory still authored by
`--from` and re-points the vector payload of every live memory `--to` owns
(idempotent; a plain re-run finds 0 contexts to move). A context `--to`
owned all along is never touched.

The command is **not fenced** against concurrent writers: a `remember` by the
`--from` identity that was authorized before the flip, or an embedding worker
that loaded the old `user_id`, can land after it. Run it while the `--from`
identity's clients (its API key, MCP sessions) are idle, then run it once more
with `--repair-payloads` to sweep anything that slipped in.

The command does not move API keys: mint a new key for `--to` if MCP clients
should keep seeing the private contexts afterwards. It also leaves other
`created_by` columns (resources, agents, files, secrets), per-user retrieval
history (neural edges, feedback, sleep reports — boosting starts over) and the
two user rows untouched.

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
  keeps every resource point of a live context. Set
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
(no row, soft-deleted, context gone), next to the number of live embedded
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
wait, up to 30 seconds; past that the pass deletes nothing and the next run
tries again. Only one API process per deployment runs the scheduled sweep.

The sweep reads Postgres one page of points at a time and ends its
transaction after each page, so a long scan is not cut short by
`idle_in_transaction_session_timeout`.

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

**What does not come back.**

- Memories the purge already removed. After the retention window the context
  comes back empty — the dry run shows the count before you apply.
- Memories forgotten before the deletion, and memories Sleep merged or archived.
  They were already deleted when the context was; the dry run lists them as
  "stay deleted".
- The context's neural edges (deleted outright; Sleep rebuilds them where it runs),
  its entries in members' context restrictions (`allowed_context_ids` — grant
  them again), and the resource tokens revoked with it.

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
  still expands `$` — and needs an explicit `REDIS_URL` (below).
- **`REDIS_URL` in `.env.prod` overrides the built URL**, whole: scheme,
  password, host and port. Set it when the password has characters a URL
  reserves (`@ : / # %` and the like — percent-encode them) or when Redis is
  somewhere else. To encode without the password landing in your shell history:
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
#    printing the secret. It should print "True True":
docker compose -f docker-compose.prod.yml --env-file .env.prod config --format json | python3 -c '
import json, sys; s = json.load(sys.stdin)["services"]
print(s["redis"]["command"][-1] != "", s["api-blue"]["environment"]["REDIS_URL"].startswith("redis://:"))'

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
