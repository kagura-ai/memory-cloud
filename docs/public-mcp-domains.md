# Verified custom domains of public MCP endpoints — Issue #2809

A workspace owner can serve a published [public MCP endpoint](deployment.md#public-mcp-endpoints--issue-2675)
under a host name they control, as `https://<host>/mcp`, once they have proved
control of the name with a DNS TXT record. The custom domain is an **alias**:
it serves the same endpoint as the fixed address `/mcp/public/<public_id>`,
with the same sessions, limits, usage budget and usage events.

This page is the contract of that feature: the states of a domain, the two DNS
records, what each request `Host` gets, the right to use it and its end, the
lease, the cooldown, the limits, the owner API and its errors, the settings,
the certificate permission call for a TLS proxy, what an operator does before
switching it on and when stopping it, and what a proxy other than the
reference one must do. The reference proxy (Caddy, single-server deployment)
is described in [`terraform/single-server/README.md`, "Custom domains (optional)"](../terraform/single-server/README.md#custom-domains-optional).

The feature is **off by default**. While `MCP_PUBLIC_DOMAINS_ENABLED` is off
the server runs no statement on the `public_mcp_domains` table, the owner
routes and the permission call answer `404`, no job is registered, and the
application applies no Host rule (see [Stopping and rolling back](#stopping-and-rolling-back)).

## Overview

1. The operator switches the feature on and runs a TLS proxy that routes alias
   names to the API (the reference proxy, or another one that follows
   [Proxies other than the reference one](#proxies-other-than-the-reference-one)).
2. The workspace's canonical owner adds a host name to a published endpoint
   (`POST …/public-mcp/domains`). The domain starts `pending`; the answer
   carries the TXT record to publish.
3. The owner publishes the TXT record and points the name at the routing
   target (`MCP_PUBLIC_DOMAINS_ROUTING_TARGET`), then calls verify. When the
   record is found, the workspace has the right and the name is free, the
   domain becomes `active` and the alias answers at `https://<host>/mcp`.
4. A scheduler job re-checks every held domain about once an hour. Each
   completed check renews a 72-hour lease; a lost proof, an ended right or a
   reserved name suspends the domain.
5. Removing the domain, or unpublishing the endpoint, releases the name. Other
   workspaces can claim it after the reassignment cooldown.

## States and transitions

| State | Holds the name | Served | Meaning |
|---|---|---|---|
| `pending` | no | no | Claimed, not yet proved. Holds nothing: several workspaces may have a `pending` row for one name. |
| `active` | yes | while `serve_until` is in the future | Proved, under an active right. |
| `suspended` | yes | no | Held but not served; `suspended_reason` is `proof_lost`, `right_ended` or `reserved`. |
| `released` | no | no | No longer held. Another workspace may claim the name once `cooldown_until` has passed; the same workspace may claim it at once. Not listed by the owner API. |

```
attach ──> pending ──verify: proof ok, right ok, name free──> active
pending ──7 days, or owner detach──> (row deleted)
active ──>= 3 confirmed absences and >= 72 h since proof_ok_at──> suspended(proof_lost)
active ──right not active at a check──> suspended(right_ended)
active ──name became reserved──> suspended(reserved)
suspended ──proof ok and right ok and not reserved (job or owner verify)──> active
suspended ──30 days since suspended_at, not reinstated at that check──> released
active | suspended ──owner detach, unpublish, endpoint or workspace row gone, operator release──> released
released ──cooldown_until passed──> (row deleted)
```

Rules a check applies to an `active` or `suspended` domain, in this order:

1. The check is always recorded: `last_checked_at`, the proof result
   (`ok`, `missing`, `unavailable`) and the routing status.
2. `ok` sets `proof_ok_at` to the check time and the count of confirmed
   absences to 0. `missing` adds one confirmed absence. `unavailable` changes
   neither.
3. An `active` domain is suspended with reason `reserved` when its name is now
   reserved; else `right_ended` when the right is not active; else
   `proof_lost` when the proof is `missing`, at least 3 absences were
   confirmed **and** at least 72 hours passed since the proof was last seen
   (`proof_ok_at`). Otherwise it stays `active` and its lease is renewed, also
   when the proof was `unavailable` or `missing` inside the grace.
4. A `suspended` domain takes reason `reserved`, else `right_ended`, as above;
   else it is reinstated (`active`, lease renewed) when the proof is `ok`; else
   its reason becomes `proof_lost` when the proof is `missing`. If it is still
   suspended and its suspension is 30 days old, it is `released` with the
   cooldown. A change of reason alone keeps `suspended_at`.

The grace starts at the last time the proof was seen, not at the first
observed absence: after a gap in checking, a proof that vanished days ago is
suspended at the third confirmed absence.

**Indeterminate lookups never change a state.** A lookup that times out, gets
`SERVFAIL` or finds no resolver configuration is `unavailable`: it is
recorded (and moves `last_checked_at`), and an `active` domain whose right is
active keeps serving with a renewed lease. A resolver outage stops no alias.
A name whose lookups never answer stays held; the operator's `release`
removes it.

**Tokens.** Every claim (`POST`) mints a new random token, also when the same
workspace claims a name again. A token belongs to one row: a value once
published proves nothing for any later row.

All times are the database clock (`timezone('UTC', statement_timestamp())`).

## The two DNS records

| Record | Name | Value | Role |
|---|---|---|---|
| TXT | `_kagura-public-mcp.<host>` | `kagura-public-mcp-verification=<token>` | **Proof of control.** Required for activation and kept published for as long as the domain is used. |
| CNAME (or A/AAAA) | `<host>` | `MCP_PUBLIC_DOMAINS_ROUTING_TARGET` (or the target's addresses) | **Routing.** Recorded for information only; never a condition of serving. |

The owner API returns both: `verification.record_name` /
`verification.record_value` and `routing.target`.

- Proof: the first 20 TXT values at the challenge name are read (a CNAME there
  is followed by the resolver); a value is the concatenation of its
  character-strings, and the proof is `ok` when one value equals the expected
  value exactly. NXDOMAIN, no answer or no equal value is `missing`. A
  timeout, `SERVFAIL`, no nameservers, a missing resolver configuration or any
  other error is `unavailable`.
- Routing: `ok` when the name's CNAME chain contains the target or the A/AAAA
  sets of the name and of the target share an address; `mismatch` when both
  resolve and neither holds; else `unresolved`. An empty routing target gives
  `unresolved`. A CDN in front of the name can make the comparison read
  `mismatch` while the alias works; it changes nothing.
- Lookups go only to the recursive resolver of the API container's system
  configuration (`/etc/resolv.conf`), with no application cache and a lifetime
  of 3 s per query; the queries of one check run concurrently, and one check
  is bounded to 8 s by its caller. The application connects to no address it
  read from DNS, makes no HTTP or TLS request to the name and queries no
  authoritative server itself. It does not validate DNSSEC.

## Host names

A claimed name is stored in one normalised form: surrounding whitespace and
one trailing dot removed, lower case. It must be ASCII (`[a-z0-9.-]`; an
internationalised name is given in its `xn--` A-label form and never shown in
Unicode), have at least two labels of 1–63 characters without a leading or
trailing hyphen, be 4–234 characters long (234 = 253 minus the challenge label
and its dot) and not end in an all-digit label. So a port, `*`, `_`, a path,
an address literal and a single label are refused.

A name is **reserved** (never claimable) when it is under a special-use suffix
(`localhost`, `local`, `internal`, `invalid`, `test`, `example`, `home`,
`lan`, `corp`, `arpa`, `onion`), or equal to or under:

- an application host (below);
- the host of `PAYMENT_PUBLIC_BASE_URL`, of any `CORS_ORIGINS` entry or of any
  `MCP_ALLOWED_ORIGINS` entry;
- `MCP_PUBLIC_DOMAINS_ROUTING_TARGET`;
- an entry of `MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES`.

Malformed and reserved names get the same answer (`422`
`public_mcp_domain_hostname_not_allowed`).

**Application hosts** are the hosts of `FRONTEND_URL`, `API_URL` (read from the
process environment), `OIDC_ISSUER` and `KMC_MCP_URL`, plus every entry of
`MCP_PUBLIC_DOMAINS_APP_HOSTS` (`name` = that host, `.name` = that host and
every name under it). Hosts of internal shape (below) are left out of the list
because they pass anyway. The list is computed from the settings on every
request (memoised on the setting values only); at start-up, with the switch
on, the log line `public_mcp_domains_application_hosts` names it (a warning
when it is empty).

## What each Host gets

The Host guard (`PublicMcpDomainHostGuard`) wraps every application
middleware: a request it does not pass on reaches no session, rate-limit,
request-logging, correlation or CORS layer. It opens no database session,
keeps no list of names and logs nothing about the request. It reads only the
`Host` header: `X-Forwarded-Host` and `Forwarded` are never read, and no URL
is ever built from the request's Host.

With `MCP_PUBLIC_DOMAINS_ENABLED=true`, per HTTP or WebSocket request:

| Request | Answer |
|---|---|
| more than one `Host` header | `400` `{"error": "bad_request"}` |
| no `Host` header, or an **internal shape**: an IPv4 or bracketed IPv6 literal, a single label (`api-blue`, `testserver`), or a name under `localhost`, `local`, `internal`, `invalid`, `test`, `example`, `home`, `lan`, `corp` or `home.arpa` | the application, unchanged |
| an application host | the application, unchanged |
| any other Host (a **foreign** one), WebSocket | closed before it is accepted (code `1008`) |
| foreign Host, `/mcp` or `/mcp/`, a claimable name | the alias (table below) |
| foreign Host, `/mcp` or `/mcp/`, a malformed or reserved name | `404` not published, no lookup |
| foreign Host, every other path and method (`/`, `/api/…`, `/internal/…`, `/mcp/public/<id>`, `/.well-known/…`, `/oidc/…`, `/docs`, `/health`) | `404` not published, no database access, no cookie, no CORS header |

The guard removes the port and one trailing dot of the `Host` value and
lower-cases it before it compares. Names under `arpa` (other than
`home.arpa`) and `onion` are foreign, never internal.

The alias on `/mcp` (and `/mcp/`, the query string kept, so `?tools=` works as
on the fixed address):

| Domain | Answer |
|---|---|
| `active`, lease current, endpoint serving | the endpoint, as on its fixed address |
| `active`, the workspace's plan pauses public serving or it is over capacity | `503` with `Retry-After: 300`, as on the fixed address |
| `pending`, `suspended`, `released`, cooling down, lease passed, unknown, reserved, endpoint unpublished, workspace deleted, `MCP_PUBLIC_ENDPOINTS_ENABLED=false` | `404` with the not-published body, identical bytes for every case |
| the lookup fails (database error) | the transport's failure answer, as on the fixed address (`503` when the database was unreachable or timed out, `500` otherwise) |

The not-published body is
`{"error": "not_found", "error_description": "No public MCP endpoint is published at this address."}`.

**Same endpoint, same counters.** The alias statement is the fixed address's
statement (endpoint not revoked; context live, public, not private and still
bound to the resource; workspace not deleted) joined to the domain row of the
same endpoint **and** workspace, with `state = 'active'` and
`serve_until` after the database clock. A name that is no longer claimable
(reserved since its domain was activated) resolves nothing, before any
statement, whatever its row says. Both return the same serving endpoint,
so from there on the two entries are one: the session principal, the
per-minute buckets (keyed by the endpoint id), the [usage budget](public-usage-budget.md)
(keyed by the workspace), the [usage events](public-mcp-usage-events.md) and
the `public_mcp_call` log line are identical. A session opened on one entry is
valid on the other. There is no per-domain bucket, counter, allowance or event
field.

**Origin.** The `Origin` check of the public transport applies unchanged (the
`MCP_ALLOWED_ORIGINS` allow-list; server-side clients send no `Origin`).
Alias origins are not added to the allow-list. On the alias entry a refusal
is logged as the fixed line `MCP request refused: Origin is not allowed`,
without the value.

## The right, the lease and the end of the right

`MCP_PUBLIC_DOMAINS_AUTHORITY` decides who has the right to custom domains:

- `entitlement` (default): the workspace's **paid integration license** with
  the reserved service id `public-mcp-domains`, through the existing guard
  ([Paid integration entitlements](ops/paid-integration-entitlements.md)): the
  workspace needs the canonical tier `promax`, a current paid Max window and a
  current window of this license. Max alone grants nothing; a sync add-on's
  license is another row. A soft-deleted workspace has no right.
- `operator`: every canonical workspace owner has the right; no publisher is
  needed. For self-hosting.

Every write of a claim, an activation and a check runs in one write
transaction under the right: under `entitlement` inside
`paid_integration_write` (a short lock on the workspace row, the right
checked again before the commit), under `operator` in an ordinary
transaction. The anonymous request path and the permission call never ask
for the right: they read the lease.

**The lease** (`serve_until`): every activation, and every completed check of
an `active` domain whose right is active, sets it to the check time plus
72 hours, under `entitlement` never past the end of the published right. It
applies **under both authorities**. No domain is `active` without a lease, and
nothing is served past it.

Policy:

- The alias serves exactly while the published right is valid. There is no
  grace in the application: a publisher that wants one publishes a longer
  window. A window that ends on schedule stops the alias at its end (the lease
  never extends past it).
- An early withdrawal of the license, or the plan leaving `promax`, reaches
  the alias at the domain's next check (within the 1-hour re-check age plus
  one 15-minute job interval), or at once when the owner calls verify. The
  domain becomes `suspended(right_ended)`.
- **If no check completes for 72 hours the alias stops by itself, under
  `entitlement` and under `operator`.** A check whose lookup was indeterminate
  is a completed check, so a resolver outage stops nothing; a stopped
  scheduler does.
- A suspended name stays held for 30 days, so that a renewed right reinstates
  it at its next check (the proof must still be published); then it is
  released with the cooldown.
- Changing the authority from `operator` to `entitlement` keeps each alias
  until its next check (about 75 minutes at most), which then applies the
  right.
- Under `entitlement` a soft-deleted workspace's aliases answer `404` at once
  (the statement requires a live workspace); its domains become
  `suspended(right_ended)` at their next check and are reinstated by the first
  check after a restore, within the 30-day hold. Under `operator` they stay
  held, unserved, until the workspace is restored or hard-deleted.
- The fixed address, the publication, its data, a sync installation and the
  usage budget's period are untouched by any of this; the fixed address has
  no entitlement check.
- The application bills nothing and counts nothing per domain. The only
  billing input is the published license window.

## Cooldown and reassignment

A domain that leaves the holding state (`active` / `suspended` → `released`)
keeps `cooldown_until` = release time + `MCP_PUBLIC_DOMAINS_REASSIGN_COOLDOWN_HOURS`
(default 168 hours). During it **another** workspace's activation of the name
is refused (`409` `public_mcp_domain_in_cooldown` with
`details.available_after`); the workspace that released it may activate it
again at once, with a new token. After the cooldown the job deletes the
released row (and with it the host name).

A name becomes held only by activation, under a lock per host name, after one
statement that reads every other holder of the name and every running
cooldown of another workspace. A unique index on held names backs it: two
workspaces verifying one name at once end with one `active` domain and one
`409`. **Held and cooldown are answered only after the caller's own proof was
found**: a caller without control of the DNS learns nothing about another
workspace. A `pending` claim of a held name stays `pending` and silent. A
holder is never displaced by another workspace's proof.

## Limits

| Limit | Value | Where |
|---|---|---|
| Domains per publication | 1 (not `released`) | unique index; `409` `public_mcp_domain_exists` |
| Domains per workspace (`pending`, `active`, `suspended`) | `MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE` (default 3) | `409` `public_mcp_domain_limit_reached` |
| Verify calls per workspace | 20 per hour | Redis counter `public_mcp_domain_verify:<workspace_id>`; `429` |
| Activation attempts per workspace | 6 per day | Redis counter `public_mcp_domain_activate:<workspace_id>`; `429` |
| Unverified claim | deleted after 7 days | job |
| Suspended hold | 30 days | job |

The verify counter counts every verify call of the canonical owner on an
existing domain, before the lookup. The activation counter counts a verify of
a `pending` domain whose proof was found, before the write transaction; a
reinstatement is not an activation, and an attempt that then ends in a `409`
still counts. Over either limit the answer is `429` `RATE-001` with a
`Retry-After` header (the counter's remaining time, else the window) and
`details.limit` = `verify_per_hour` or `activations_per_day`. When Redis
cannot be read the action is refused (`503`
`public_mcp_domain_verification_unavailable`): the counters never fail open.

An operator who raises the per-workspace limit should know that onboarding
more than 6 names takes more than a day.

## Owner API

Mounted under `/api/v1`. Every route answers `404` `RES-001` before any
statement while `MCP_PUBLIC_DOMAINS_ENABLED` is off, and sends
`Cache-Control: no-store` (refusals included), except the generic
request-validation `422` of a request that fails validation (see "Errors").
The workspace comes from the
authenticated principal (session or owner API key), the publication from
`(workspace, resource slug)`; every row query is scoped by the workspace and
the publication.

| Route | Who | Success |
|---|---|---|
| `GET /resources/{resource_id}/public-mcp/domains` | any workspace owner | `200` `{"domains": [domain, …]}` (rows not `released`, oldest first) |
| `POST /resources/{resource_id}/public-mcp/domains` body `{"hostname": "<name>"}` (1–255 characters) | the canonical owner | `201` domain (`pending`) |
| `POST /resources/{resource_id}/public-mcp/domains/{domain_id}/verify` | the canonical owner | `200` domain |
| `DELETE /resources/{resource_id}/public-mcp/domains/{domain_id}` | any workspace owner | `204` |

The **canonical owner** is the workspace owner whose user id is the
workspace's `owner_user_id`; another owner member gets `403`
`public_mcp_domain_owner_required` on add and verify, before any counter or
lookup.

**Add** checks the owner, then the name (`422` when malformed or reserved),
then the live publication (`404`), then, in the write transaction under the
right: the owner again, the publication again (held against a concurrent
unpublish), the workspace limit and the one-domain-per-publication rule. It
reads no other workspace's rows. A name another workspace holds is accepted as
a `pending` claim.

**Verify** checks the owner, reads the domain (`404` when it is not an open
domain of this workspace and publication), counts the verify call, then looks
the records up with no database connection held. Then:

- proof `unavailable`: `503` `public_mcp_domain_verification_unavailable`,
  nothing written (every state);
- `pending`, proof `missing`: the check is recorded, `200` with the domain
  still `pending` (`last_check.result: "missing"`);
- `pending`, proof `ok`: the activation counter, then the write transaction
  under the right (`403` `public_mcp_domain_not_entitled` when refused; the
  domain stays `pending`), the owner again, the per-name lock, the
  publication still the bound one (`409` `public_mcp_endpoint_not_published`),
  the name still claimable (`422`), no other holder and no running cooldown of
  another workspace (`409` `public_mcp_domain_held` /
  `public_mcp_domain_in_cooldown`), then `active` with a lease;
- `active` or `suspended`: the same check the job runs (rules above), `200`
  with the result. When the right is not active, the suspension is written
  first and the answer is `403` `public_mcp_domain_not_entitled`.

A result is applied only to the version of the domain the lookup started
from; when it changed meanwhile the answer is `409` `public_mcp_domain_changed`
(retry).

**Remove** needs no right. A `pending` domain is deleted; an `active` or
`suspended` one is `released` with the cooldown. The alias answers `404` from
the next request, a session opened before included, and the permission call
answers `404`.

**Unpublish** of the endpoint (`DELETE /resources/{resource_id}/public-mcp`)
releases its domains in the same transaction while the switch is on: the
`pending` rows are deleted, then the holders are released with the cooldown.
A later republish gets a new endpoint and no alias; the owner adds the domain
again (with a new token).

### Domain object

```json
{
  "domain_id": "8d0f6c1e-2c4b-4c47-9f39-3a3a1c1f2b10",
  "hostname": "mcp.example.org",
  "url": "https://mcp.example.org/mcp",
  "state": "active",
  "suspended_reason": null,
  "serving": true,
  "verification": {
    "record_type": "TXT",
    "record_name": "_kagura-public-mcp.mcp.example.org",
    "record_value": "kagura-public-mcp-verification=3f1c…"
  },
  "routing": {"target": "mcp-domains.example.com", "status": "ok"},
  "last_check": {"result": "ok", "checked_at": "2026-11-15T09:00:00Z"},
  "proof_ok_at": "2026-11-15T09:00:00Z",
  "proof_missing_checks": 0,
  "serve_until": "2026-11-18T09:00:00Z",
  "created_at": "2026-11-15T08:40:00Z",
  "verified_at": "2026-11-15T08:45:00Z"
}
```

| Field | Meaning |
|---|---|
| `state` | `pending`, `active` or `suspended` |
| `suspended_reason` | `proof_lost`, `right_ended`, `reserved`, or `null` |
| `serving` | whether the alias answers right now (the alias statement resolves this domain and the name is still claimable; a plan pause or capacity lock, answered `503`, does not change it) |
| `verification` | the TXT record to publish |
| `routing.target` | `MCP_PUBLIC_DOMAINS_ROUTING_TARGET`; `routing.status` is `ok`, `mismatch`, `unresolved` or `null` before the first check |
| `last_check` | result (`ok`, `missing`, `unavailable`) and time of the last recorded check, `null` before it |
| `proof_ok_at`, `proof_missing_checks` | last time the proof was found; confirmed absences since then |
| `serve_until` | the lease; the alias is served only before it |

Times are UTC with a `Z` suffix.

### Errors

Envelope `{"error": …, "message": …, "details": {…}}`. No answer names
another workspace.

| Status | `error` | When |
|---|---|---|
| `404` | `RES-001` | switch off; no live publication for the resource; the domain is not an open domain of this workspace and publication (or it is `released`) |
| `403` | `public_mcp_domain_owner_required` | add or verify by an owner member who is not the canonical owner |
| `403` | `public_mcp_domain_not_entitled` | authority `entitlement` and the right is not active (an `active` domain is suspended first) |
| `422` | `public_mcp_domain_hostname_not_allowed` (`details.field: "hostname"`) | malformed or reserved name; the message is always "This host name cannot be used as a custom domain." |
| `409` | `public_mcp_domain_limit_reached` | the workspace's limit |
| `409` | `public_mcp_domain_exists` | the publication already has a domain |
| `409` | `public_mcp_endpoint_not_published` | the publication changed during verify |
| `409` | `public_mcp_domain_held` | another domain holds the name (only after the caller's own proof was found) |
| `409` | `public_mcp_domain_in_cooldown` (`details.available_after`, ISO UTC with `Z`) | another workspace released the name recently (only after the caller's own proof was found) |
| `409` | `public_mcp_domain_changed` | the domain changed during the check; retry |
| `409` | `public_mcp_domain_command_stale` | installation-bound operations only (experimental): the binding or the named domain is no longer as the command sent it (see "Installation-bound owner operations") |
| `429` | `RATE-001` (`Retry-After` header, `details.retry_after`, `details.limit`) | over 20 verify calls per hour or 6 activation attempts per day |
| `503` | `public_mcp_domain_verification_unavailable` | the lookup was indeterminate, or a counter could not be read |

A request that fails validation (a body with no `hostname`, an empty one or
one over 255 characters; a `domain_id` that is not a UUID; a `resource_id`
outside the slug pattern or over its length limit) gets the generic
request-validation `422`, without `Cache-Control: no-store`.

### Capability

`GET /api/v1/system/info` reports `features.public_mcp_domains`: `true` while
`MCP_PUBLIC_DOMAINS_ENABLED` is on. It says only that the deployment offers
the feature, never which hosts it uses.

For an installation's browser client, `GET /.well-known/kmc-sync-configuration`
lists `public.domains.observation.v1` (reading the installation's domain) and,
while `SYNC_PUBLICATION_DOMAIN_OPERATIONS_ENABLED` is also on,
`public.domains.operations.v1` (claim, verify and remove; experimental). See
the two "Installation-bound" sections below.

## Certificate permission call

`GET /internal/public-mcp-domains/tls-permission?domain=<name>` answers a TLS
proxy that obtains certificates on demand whether a name is an alias the
application serves now. It has **no credential** (a proxy's permission call
carries none) and is not in the OpenAPI document. It lives under `/internal`,
which must stay unreachable from outside (the reference proxy answers `404`
for `/internal*`, and `deploy.sh` checks it on every deploy); the Host guard
also answers `404` for every `/internal` path under a foreign Host, so the
proxy calls it on the internal network under an internal Host, for example
`http://api-blue:8080/internal/public-mcp-domains/tls-permission`.

Every answer has an empty body:

| Status | When |
|---|---|
| `200` | the name is an `active` alias with a current lease, of a live publication, and `MCP_PUBLIC_DOMAINS_ENABLED` and `MCP_PUBLIC_ENDPOINTS_ENABLED` are both on |
| `404` | either switch off; no `domain`, or more than one; a malformed, special-use, application or reserved name; a name that is not served |
| `503` | all 4 lookup slots of this process are busy (a call never waits for one); the lookup took longer than 3 s; a database or other error. A proxy reads it as a refusal. |

The switches, the name and the free slot are decided before any database
session is opened: steps that answer `404` or the busy `503` run no
statement. Plan pause and capacity lock do not withdraw the permission (the
alias answers `503` at request time instead). No log line carries the name:
the query of this path is replaced with `domain=[REDACTED]` in every rendered
log line, uvicorn's access line included. The busy case logs
`public_mcp_domain_permission_busy`, a failure
`public_mcp_domain_permission_failed` with the error type only.

## Periodic work

The scheduler job `public_mcp_domain_maintenance` runs every 900 s (15
minutes) and once when the scheduler starts. It is registered only while
`MCP_PUBLIC_DOMAINS_ENABLED` is on, by the scheduler leader. A run:

1. **Housekeeping**, four statements of at most 500 rows each, each in its own
   short transaction, skipping rows another transaction holds:
   - `pending` rows older than 7 days are deleted;
   - `released` rows whose cooldown has passed are deleted (no audit row: the
     release was audited);
   - rows that are not `released` whose workspace row is gone, or whose
     publication is gone, revoked or of another workspace: `pending` rows are
     deleted, held rows are released with the cooldown.
2. Reads up to 200 held rows whose last check is older than 1 hour (or that
   were never checked): rows whose last lookup was `unavailable` after all
   others, then the oldest check first. The connection is released.
3. Checks them, 4 at a time: the lookup outside any session, then the result
   applied by the same writer an owner's verify uses. Every completed lookup is
   applied, `unavailable` included. No check starts once 120 s have passed
   since the run started; the rest is counted `budget_skipped` and stays due.
4. While edge certificate reports are configured, deletes up to 500 reports
   whose `valid_until` has passed, in a session of its own (not counted in the
   job status). A failure there is logged as
   `public_mcp_domain_tls_report_purge_failed` and never stops the run or the
   checks before it.

The job status (`GET /api/v1/admin/operations/status`, see
[Operations status](deployment.md#operations-status-scheduled-jobs-backlogs-llm-lanes--issue-2768))
shows the run's integer counts: `checked`, `proof_ok`, `proof_missing`,
`dns_unavailable`, `suspended`, `reinstated`, `released`, `pending_expired`,
`orphans_released`, `purged`, `fenced` (the domain changed during its lookup;
nothing written), `budget_skipped`. A run that fires while the switch is
already off returns `{"skipped": 1}`. The first database error ends the run;
the job is recorded as failed (`public_mcp_domain_maintenance_failed` with
the error class).

**Without a running scheduler leader no check completes, and every alias
stops when its 72-hour lease passes, under either authority.**

## Operator command

Run where the API runs (same environment), for example in the active API
container:

```bash
python -m src.cli.public_mcp_domains list [--state pending|active|suspended|released] [--workspace <workspace-id>]
python -m src.cli.public_mcp_domains show --domain-id <id>
python -m src.cli.public_mcp_domains show --hostname <name>
python -m src.cli.public_mcp_domains release --domain-id <id>                                # plan
python -m src.cli.public_mcp_domains release --domain-id <id> --apply --yes                  # release
python -m src.cli.public_mcp_domains release --domain-id <id> --skip-cooldown --apply --yes  # delete
```

- `list` and `show` never write. `show --hostname` lists every row of the name
  (the verification token is not printed).
- `release` plans by default. With `--apply`: an `active` or `suspended` row
  is released with the reassignment cooldown, as an owner's remove does; a
  `pending` row is deleted; a `released` row is left alone. `--skip-cooldown`
  deletes the row instead (also a `released` one still in its cooldown). It
  deletes this row only: another workspace can claim the name once no other
  row of it is held or still in its cooldown. A workspace that detached a name,
  took it back and detached it again leaves two `released` rows; check
  `show --hostname` and run the command for each row that still holds the name.
- Each change writes one audit row `public_mcp_domain.released` with reason
  `operator` and the actor `cli:public-mcp-domains`.
- Exit codes: `0` ok, `1` error (no such domain), `2` usage (`--yes` without
  `--apply`, a malformed `--hostname`).
- It works while `MCP_PUBLIC_DOMAINS_ENABLED` is off: use it to remove host
  names after switching the feature off, since no job runs then.
- Host names are printed to the terminal, never written to a log line.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `MCP_PUBLIC_DOMAINS_ENABLED` | `false` | The switch. Off: no statement on `public_mcp_domains`, owner routes and permission call `404`, no job, **no Host rule**. On: needs `MCP_PUBLIC_DOMAINS_ROUTING_TARGET` (start-up refuses otherwise). Allowed without `MCP_PUBLIC_ENDPOINTS_ENABLED` (every alias then answers `404`). |
| `MCP_PUBLIC_DOMAINS_AUTHORITY` | `entitlement` | Who has the right: `entitlement` (the license `public-mcp-domains`) or `operator` (every canonical owner). |
| `MCP_PUBLIC_DOMAINS_ROUTING_TARGET` | *(empty)* | The name owners point their host at (CNAME target). A host name outside the special-use suffixes. Reserved for claims. |
| `MCP_PUBLIC_DOMAINS_APP_HOSTS` | *(empty)* | Further names the application is reached under, comma-separated: `name` = that host, `.name` = that host and every name under it. An entry is a bare host name of two or more labels, with no scheme and no port (a zone entry too: `.default.svc`, not `.svc`); any other entry refuses start-up. Reserved for claims. |
| `MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES` | *(empty)* | Names never claimable, nor any name under them, comma-separated (no leading dot). Refused on the request path from the first request after a restart, whatever the domain's row says; the next check suspends the domain (`reserved`). |
| `MCP_PUBLIC_DOMAINS_REASSIGN_COOLDOWN_HOURS` | `168` | Hours a released name stays closed to other workspaces (0–2160). |
| `MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE` | `3` | Domains (`pending`, `active`, `suspended`) per workspace (1–50). |
| `MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN` | *(empty)* | Bearer credential of the edge certificate reporter ("Edge certificate reports"). Empty: every report is `404`, nothing reads or writes `public_mcp_domain_tls_reports`, every certificate is `unreported`. No blanks; under `ENVIRONMENT=production` at least 32 characters and not another service credential. Never logged. |
| `SYNC_PUBLICATION_DOMAIN_OPERATIONS_ENABLED` | `false` | Experimental. The owner's claim, verify and remove through an installation's browser client ("Installation-bound owner operations"), advertised as `public.domains.operations.v1`. Takes effect only while `public.domains.observation.v1` is advertised (`OIDC_ENABLED`, `MCP_PUBLIC_ENDPOINTS_ENABLED`, `SYNC_INTEGRATIONS_ENABLED`, `MCP_PUBLIC_USAGE_BUDGET_ENABLED` and this file's switch); off: the three routes answer `404` before authentication and run no statement. No cross-check at start-up, so turning `MCP_PUBLIC_DOMAINS_ENABLED` off alone still closes everything. |

A malformed host name in the routing target, an application host or a
reserved suffix stops the process at start-up; the error names the variable,
never the value. The values are normalised (lower case, one trailing dot
removed).

Fixed values (code constants, repeated here): license id `public-mcp-domains`;
challenge label `_kagura-public-mcp`; value prefix
`kagura-public-mcp-verification=`; proof grace 72 hours and 3 confirmed
absences; suspended hold 30 days; pending claims 7 days; lease 72 hours;
re-check age 3,600 s; job interval 900 s, batch 200, concurrency 4,
housekeeping 500 rows per statement, run budget 120 s; one check 8 s, one DNS
query 3 s, 20 TXT values; 20 verify calls per workspace per hour; 6 activation
attempts per workspace per day; host names 4–234 characters; permission call 4
concurrent lookups per process and 3 s.

## Before switching on

1. **Run the migration first.** `alembic upgrade head` runs
   `f51_2809_public_mcp_domains` (it follows the merge revision
   `f50_2807_merge_sync_heads`, beside `f51_2808_managed_sync`; the no-DDL
   merge revision `f52_2808_merge_feed_domains` joins the two and is the
   single head): one new, empty table and its indexes. It has
   no foreign key and no statement on any existing table, so it locks no
   existing table. (The same `alembic upgrade head` runs
   `f51_2808_managed_sync`, whose foreign keys take locks on
   `sync_installations`, `users` and `memories`, and on the feed tables
   `sync_feeds` and `sync_runs`.) Run it before
   `MCP_PUBLIC_DOMAINS_ENABLED=true`.
2. **Application hosts.** Once the switch is on, a request under a Host that is
   neither an application host nor an internal shape is treated as an alias
   candidate: everything but `/mcp` answers `404`. List every name the API is
   reached under that `FRONTEND_URL`, `API_URL`, `OIDC_ISSUER` and
   `KMC_MCP_URL` do not already name in `MCP_PUBLIC_DOMAINS_APP_HOSTS`
   (internal service names with dots included). Check the start-up line
   `public_mcp_domains_application_hosts`.
3. **Reserved suffixes.** Put your own registrable domain and sibling hosts in
   `MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES`.
4. **Routing target.** Create the record owners point their names at (it must
   reach the proxy directly, not through a CDN that holds no certificate for
   their names) and set `MCP_PUBLIC_DOMAINS_ROUTING_TARGET`.
5. **Resolver.** The API container's resolver configuration must reach a
   recursive resolver you trust; the application does not validate DNSSEC.
6. **Scheduler leader.** Confirm that a scheduler leader runs and that
   `public_mcp_domain_maintenance` appears in the job status: without it every
   alias stops after 72 hours.
7. **Right.** Under `entitlement`, publish the license `public-mcp-domains`
   for the workspaces that bought it; under `operator`, nothing.
8. Switch the feature on, **then** enable alias routing at the proxy (the
   reverse of the stop order): a new name gets no certificate while the switch
   is off, because the permission call answers `404` then, but a name whose
   certificate is still stored from an earlier run would be routed at once.

## Stopping and rolling back

Take the alias routing out of the proxy **first**, **then** switch the feature
off (`MCP_PUBLIC_DOMAINS_ENABLED=false`) or roll back to a release without it.
While the switch is off, and on a release without the feature, the
application applies no Host rule: a request under an alias name that still
reaches it is treated like any other host, and on `/mcp` that is the
authenticated transport (401 with a sign-in challenge).

The ways to stop aliases:

- one name or one zone, at once and reversibly: add it to
  `MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES` and restart (the guard refuses it; the
  row is held as `suspended(reserved)` from its next check);
- one name for good: the operator CLI's `release`
  (`python -m src.cli.public_mcp_domains release --domain-id <id>` shows the
  plan, `--apply --yes` acts);
- every alias while the proxy still routes alias names: keep
  `MCP_PUBLIC_DOMAINS_ENABLED=true` and set
  `MCP_PUBLIC_ENDPOINTS_ENABLED=false` (this stops the fixed addresses too);
- switching the feature off or rolling back: take the alias routing out of the
  proxy first (reference proxy: restore the template, render, recreate the
  proxy), then switch off or roll back.

While the switch is off no job runs: host names of released rows stay until
the operator removes them with the CLI or switches the feature on again, and
no lease is renewed (an alias whose lease passed meanwhile serves again only
after its first check). The migration's downgrade drops the table and every
binding with it. Name the target: at the head `f52_2808_merge_feed_domains`,
`alembic downgrade -1` is refused (two parents), and
`alembic downgrade f51_2809_public_mcp_domains` only unmerges `f52_2808`
(no DDL): the table and every binding stay. To drop this table alone, use
`alembic downgrade f51_2809_public_mcp_domains@f50_2807_merge_sync_heads`:
it unmerges `f52_2808`, reverts `f51_2809` alone, drops `public_mcp_domains`
with every binding and leaves the database at `f51_2808_managed_sync`;
`alembic upgrade head` applies `f51_2809` and the merge again. A plain
`alembic downgrade f50_2807_merge_sync_heads` reverts both `f51` revisions:
besides `public_mcp_domains` it drops the five managed-feed tables, which the
v0.164.0 application reads on every content write (see
[Feed ownership and safe manual operation](ops/feed-managed-sync.md)), and
every downgrade to a lower revision does both first. Switch the feature off,
after the proxy step above, before any of them.

## What the application does not guarantee

These are the operator's duties:

1. Certificates exist and renew: the proxy, the CA, ingress for the
   challenge, the customers' `CAA` records, the CA's rate limits.
2. The proxy asks the permission call before it obtains a certificate, keeps
   `/internal/*` off the internet, serves only `/mcp` on alias hosts, forwards
   the client's `Host` unchanged, refuses a request whose `Host` differs from
   the TLS server name, and removes `Cookie` and `Authorization` on the way up
   and `Set-Cookie` on the way down on alias hosts.
3. The web UI is another container: only the proxy keeps it off an alias host.
4. While the switch is off, and on a release without the feature, the
   application applies no Host rule. Alias routing is removed at the proxy
   before switching off or rolling back.
5. The resolver's integrity (no DNSSEC validation by the application).
6. Re-checks, the end of a right and the renewal of the lease need a running
   scheduler leader. Without it every alias stops when its 72-hour lease
   passes, under either authority.
7. Proxy logs name customer hosts; stored certificates of removed names stay
   in the proxy's storage until they expire or the operator deletes them.

## Proxies other than the reference one

A proxy in front of the API serves aliases correctly when it:

- routes only `/mcp` and `/mcp/` on alias names, to the API, and answers
  everything else on those names itself (`404`);
- obtains a certificate for a name only when the permission call answers `200`
  (or drives issuance from the operator CLI's `list --state active`);
- forwards the client's `Host` unchanged, and never derives `Host` from a
  forwarded header;
- refuses a request whose `Host` differs from the TLS server name of the
  connection (this covers `localhost`, an address, a trailing dot and a
  missing `Host`);
- removes `Cookie` and `Authorization` on the way to the API and `Set-Cookie`
  on the way back;
- keeps `/internal/*` unreachable from outside;
- streams responses (no buffering) and allows long-lived connections, as the
  fixed address needs.

A proxy that terminates TLS at an edge for customer names does not need the
permission call; the edge's own hostname validation runs beside the TXT proof,
which stays authoritative for the application.

## Data lifecycle

One row per claim in `public_mcp_domains`: the host name, the workspace and
publication ids, the verification token, the state, the lease, the check
results and times. No person column and no foreign key. The host name lives on
the row and is deleted with it (a `pending` row after 7 days or on remove, a
`released` row when its cooldown ends), and in the claim's edge certificate
report until the maintenance pass deletes that report (below). The table is
operational data: never exported, nothing in it for account erasure.

- Workspace soft delete: aliases answer `404` at once (see the right above).
- Workspace hard delete: the rows stay (no foreign key); the job releases held
  rows with the cooldown and deletes claims, then deletes the released rows
  when the cooldown ends.
- Unpublish: released in the same transaction (above).
- Edge certificate reports (`public_mcp_domain_tls_reports`): one row per claim
  id with the host name the report named, no person column and no foreign key;
  never shown for a released or removed claim. The maintenance pass deletes a
  report once past its `valid_until`, and only while reporting is configured:
  after the credential is emptied (or a switch is turned off) the remaining
  rows are neither shown nor deleted until it is configured again. To clear
  them: `DELETE FROM public_mcp_domain_tls_reports;`.

## Audit and log events

One audit row per transition, in the transition's transaction: action
`public_mcp_domain.attached|verified|suspended|reinstated|detached|released`,
`resource = "public_mcp_domain:<domain_id>"`, metadata `{workspace_id,
endpoint_id, from_state, to_state, reason, hostname_sha256}`. The host name
itself is never in an audit row: the SHA-256 lets an operator match a known
name after the row is gone. Actor columns: an owner action writes the user's
id and e-mail; the job writes `system:public-mcp-domains`, the command line
`cli:public-mcp-domains` (both columns). `released` carries the reason
`unpublished`, `expired` (a claim older than 7 days), `orphaned` (workspace or
publication gone), `operator` (the CLI), or `null` (a 30-day suspension
ended); `suspended` carries the suspension reason. The audit row of an
installation-bound command also carries `client_id` (the browser client) and
`installation_id` in its metadata.

| Event | Level | Fields | When |
|---|---|---|---|
| `public_mcp_domains_application_hosts` | info (warning when empty) | `hosts` | start-up, switch on |
| `public_mcp_domain_host_guard_registered` | info | — | start-up |
| `public_mcp_domain_attached` … `_released` | info | `domain_id`, `workspace_id`, `endpoint_id`, `from_state`, `to_state`, `reason` | each transition (as the audit row) |
| `public_mcp_domain_counter_unavailable` | warning | `error_type` | a Redis counter could not be read |
| `public_mcp_domain_permission_busy` | warning | — | a permission call found every lookup slot busy |
| `public_mcp_domain_permission_failed` | warning | `error_type` | a permission lookup failed or timed out |
| `public_mcp_domain_maintenance_run` | info | the counts | each job run |
| `public_mcp_domain_maintenance_failed` | warning | `error_class` | a job run raised |
| `scheduled_public_mcp_domain_maintenance` / `public_mcp_domain_maintenance_disabled` | info | `interval_seconds` / — | job registration at scheduler start |
| `public_mcp_alias_not_served` | debug | — | an alias lookup found nothing |
| `public_mcp_domain_tls_report_stored` | info | `domain_id`, `status` | an edge certificate report was stored |
| `public_mcp_domain_tls_report_refused` | warning (`disabled`: debug) | `reason`, `domain_id` (when known) | an edge certificate report was refused |
| `public_mcp_domain_tls_report_purge_failed` | error | `error_type` | the maintenance pass could not delete expired reports; the run goes on |

No log line, usage event, job count or audit row carries a host name, a token,
a DNS answer, a resolved address or the `Origin` of an alias request.

### Installation-bound owner observation

Public discovers `public.domains.observation.v1` alongside `public.domains.v1`
and reads `GET /api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/publication-domains`
with the registered browser client's `workspaces:read` OIDC access token.
The current canonical owner receives the subject, application, installation,
generation, and exact resource, context and endpoint identifiers. Clients
must match the entire binding and discard the receipt at `valid_until`.
The maximum lifetime is 30 seconds and a positive serving observation ends
by the lease deadline. A released row exposes only release and cooldown
metadata during its remaining cooldown. A missing binding has no domain
facts. DNS proof, routing, and serving are independent. TLS is
`{"status":"unreported"}` unless a current certificate report of the edge
names this claim ("Edge certificate reports").

### Installation-bound owner operations

Experimental (#2921): the three routes, their bodies and answers, the error
code `public_mcp_domain_command_stale`, the capability and the switch may
change in a minor release.

The workspace's canonical owner can also claim, verify and remove the custom
domain of an installation's public endpoint through the installation's
registered browser client. Discovery advertises `public.domains.operations.v1`
while `SYNC_PUBLICATION_DOMAIN_OPERATIONS_ENABLED` is on and
`public.domains.observation.v1` is advertised; otherwise the three routes
answer `404` before authentication and run no statement. A client decides
only from the capability and treats any `404` on these routes as "not
available": a server without these routes also answers `404`, with the global
error envelope instead of `{"error": …}`.

| Route | Body | Success |
|---|---|---|
| `POST /api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/publication-domains/claim` | `{"expected": …, "hostname": "<name>"}` | `200`, `domain` = the new `pending` claim |
| `POST …/publication-domains/verify` | `{"expected": …, "domain": {"domain_id": …, "state": …}}` | `200`, `domain` = the state the check left; `null` when the check released the domain |
| `POST …/publication-domains/remove` | as verify | `200`, `domain: null` |

**Who.** The current canonical owner (the workspace's `owner_user_id` and an
`OWNER` member) with an access token of the installation's registered browser
client whose token, grant and client carry `integrations:manage` (the
Authorization Code + PKCE flow with `scope=openid integrations:manage`). A
`workspaces:read` token reads the observation and gets `403`
`insufficient_scope` here. KMC does not bind a token to one operation: a token
with `integrations:manage` for that client also authorizes unlinking the
installation and the mode controls, so asking for one authorization per
operation is the client's obligation.

**Bodies.** `expected` is the binding the client last read in the
observation, all seven fields: `application_id`, `resource_id`,
`resource_pk`, `context_id`, `endpoint_id`, `public_id`, `generation`. Verify
and remove also name the domain and the state it was read in (`pending`,
`active` or `suspended`). An unknown or repeated key, a missing field, a
`domain` in a claim, an unknown query parameter or a body that is not JSON is
`400` `invalid_request`; a body over 8,192 bytes is `413` `request_too_large`.

```json
POST /api/v1/workspaces/3f0c2a7e-5a52-4a0e-9a3e-2f3d6c1b7a10/sync-installations/9a1e4c22-6d1b-4f0a-8a0e-0d2b6f1c9e77/publication-domains/claim
Authorization: Bearer kmc_oidc_at_...
Content-Type: application/json

{
  "expected": {
    "application_id": "0b7c9a52-1f6e-4a3b-9c1d-7e2f4a6b8c90",
    "resource_id": "sync-5d0c7d0e1f3b4a6c8e9f0a1b2c3d4e5f",
    "resource_pk": "5d0c7d0e-1f3b-4a6c-8e9f-0a1b2c3d4e5f",
    "context_id": "c1a2b3c4-d5e6-4f70-8a9b-0c1d2e3f4a5b",
    "endpoint_id": "e0f1a2b3-c4d5-4e6f-8a7b-9c0d1e2f3a4b",
    "public_id": "pmcp_AbCdEfGhIjKlMnOpQrStUv",
    "generation": 1
  },
  "hostname": "mcp.example.org"
}

200
{"schema_version": "public-domain-command-v1", "action": "claim",
 "domain": {"domain_id": "8d0f6c1e-2c4b-4c47-9f39-3a3a1c1f2b10", "state": "pending"}}
```

```json
POST .../publication-domains/verify   (remove takes the same body)

{"expected": { ...the seven fields... },
 "domain": {"domain_id": "8d0f6c1e-2c4b-4c47-9f39-3a3a1c1f2b10", "state": "pending"}}

verify 200  {"schema_version": "public-domain-command-v1", "action": "verify",
             "domain": {"domain_id": "8d0f6c1e-2c4b-4c47-9f39-3a3a1c1f2b10", "state": "active"}}
remove 200  {"schema_version": "public-domain-command-v1", "action": "remove", "domain": null}
```

The answer is a receipt of what the command left behind; its `domain` is what
the client sends as `domain` in its next command. The TXT record, the
routing, the lease and the last check are read from the observation. Every
answer carries `Cache-Control: no-store`.

**Send the binding and the domain you last read; after any `409`, read the
observation again.** A command is applied only to the state it names: the
installation's binding as sent and, for verify and remove, the named domain as
an open domain of this publication in the named state; a claim needs a
publication with no open domain. KMC checks the caller and the binding before
anything else (a caller who is not admitted consumes no counter, starts no
lookup and meets no paid check), and again as the first locking statement of
every write transaction, with the workspace's ownership epoch of the first
check. When the binding or the domain is no longer as sent, or the ownership
changed and changed back during the command, the answer is `409`
`public_mcp_domain_command_stale` and the domain is unchanged; a verify whose
check releases a domain that has been suspended for 30 days answers
`domain: null`, the same release the job makes. After any `409` the client
reads the observation again and decides from what it shows.

Unlink, reconnect, every stop of managed synchronization (by the owner, the
billing service or the maintenance pass) and resume advance the installation's
`generation`, so a command formed before any of them is stale. The
installation's `state` is not a condition: an unlinked installation keeps its
endpoint and its alias, and its current binding is accepted. An unpublished or
republished endpoint is `404` here; this interface never adopts the domain of
a new publication (the owner API manages it).

Errors (envelope `{"error": "<code>"}`, without `message` or `details`):

| Status | `error` | When | Routes |
|---|---|---|---|
| `400` | `invalid_request` | an unknown or repeated query parameter; not JSON; a malformed body, an unknown key, a missing field, a `domain` in a claim, `state: "released"` | all |
| `413` | `request_too_large` | a body over 8,192 bytes | all |
| `401` | `invalid_token` | a missing, malformed, expired or revoked token, or one that expires before the commit | all |
| `403` | `insufficient_scope` | the token, its grant or its client lacks `integrations:manage` | all |
| `403` | `SLOT-001` | the canonical owner of a slot-suspended workspace | all |
| `404` | `not_found` | the switch or a prerequisite off; not the current canonical owner or not an `OWNER` member; the workspace missing or deleted; no such installation of this workspace for this client and service generation; the installation's target not live and exact | all |
| `409` | `public_mcp_domain_command_stale` | the binding is not the installation's current one; the named domain is not an open domain of this publication, or not in the named state; the ownership changed and changed back during the command. Answered only before the command writes a domain row | all (the domain cases: verify, remove) |
| `409` | `RES-002` | an authority row is held by another transaction (send the same command again); the client has no enabled service registration | all |
| `409` | `public_mcp_domain_exists` | the publication already has an open domain | claim |
| `409` | `public_mcp_domain_limit_reached` | the workspace's number of domains | claim |
| `422` | `public_mcp_domain_hostname_not_allowed` | a malformed or reserved name | claim, verify (the name became reserved) |
| `403` | `public_mcp_domain_not_entitled` | authority `entitlement` and no active right (a verify of an `active` domain writes the suspension first; of a domain suspended for 30 days, the release) | claim, verify |
| `409` | `public_mcp_domain_changed` | the domain changed during the lookup; the same command can be sent again | verify |
| `409` | `public_mcp_endpoint_not_published`, `public_mcp_domain_held`, `public_mcp_domain_in_cooldown` | as in the owner API, only after the caller's own proof was found | verify |
| `429` | `RATE-001`, `Retry-After` header | over 20 verify calls per hour or 6 activation attempts per day of the workspace | verify |
| `429` | `temporarily_unavailable`, `Retry-After` header | the OIDC burst limiter (bucket `sync-human`) | all |
| `503` | `public_mcp_domain_verification_unavailable` | an indeterminate lookup (nothing written) or a counter that cannot be read | verify |
| `503` | `temporarily_unavailable` | any other failure | all |

Duplicates and retries (nothing is stored per command and there is no
operation id; a different body is a different command, judged on its own
state):

| Case | Claim | Verify | Remove |
|---|---|---|---|
| The same command again after it was applied (also a retry after a lost answer) | `409` `public_mcp_domain_exists` (or `public_mcp_domain_limit_reached` when the first claim filled the limit); one domain, its TXT value unchanged | the first made the domain `active` or released it: `409` stale, before any counter and lookup; the first left it in the same state: a new check, counted | `409` stale (the domain is no longer open) |
| Two identical commands at once | one `200`; the other `409` `public_mcp_domain_exists` or `RES-002` | one result is applied; the other `409` `public_mcp_domain_changed`, stale or `RES-002` | one `200`; the other `409` stale or `RES-002` |

How a client reconciles after a lost answer or a `409`, from the observation:
a claim was applied when the publication has an open domain with that host
name (a `409` `public_mcp_domain_exists` or `public_mcp_domain_limit_reached`
after a lost answer can be the client's own success); a remove was applied
when the id is no longer an open domain; for a verify the client reads the
state again. Once a client has sent a remove it does not send an earlier,
unanswered claim again: a claim sent again after its removal creates a new
`pending` claim with a new TXT value, which holds the publication's one open
domain and counts toward the workspace's limit until it is removed or deleted
after 7 days. A remove always names one domain id, never "the current domain"
or a host name, so a retried remove of an earlier domain never touches a newer
claim of the same name.

The limits are the owner API's, shared with it: one open domain per
publication, `MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE`, and the same two counters
of the workspace. The verify call is counted after the caller was admitted and
the named domain matched, before the lookup; a refused caller, a stale command
and a `404` count nothing and start no lookup (a change of the binding or the
token between the first check and the read of the domain still counts that one
call). The activation attempt is counted in the write transaction, after the
caller was admitted again, so a busy authority row (`409` `RES-002`) or a
refused admission counts nothing; the owner API counts it before its write
transaction.

Each transition writes the owner API's audit row, with the token's user as
the actor; its metadata also carries `client_id` (the browser client) and
`installation_id`. A command that changes nothing writes no domain audit row.
A `401`, `403` or `404` of a caller whose token was already verified is
recorded as `sync.authority.denied` (best effort, after the rollback), as on
every synchronization control route; a `409` or `429` writes none.

### Edge certificate reports

The certificate operator of the edge (#2941) reports what it observed about the
certificate of one claim; the owner's installation-bound observation shows the
report as the domain's `tls`. A report never authorizes serving, never changes
the domain row, the standard endpoint, the permission call or the right, and KMC
never holds a certificate's private key.

**The route.** `PUT /internal/public-mcp-domains/tls-observation`, one
`Authorization: Bearer <MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN>` header, a JSON
body of at most 4,096 bytes. It is internal: the same ingress rule that answers
`404` for every `/internal` path from outside keeps it private, and the reporter
reaches the API on the private network under an application or internal Host,
for example `http://api-blue:8080/internal/public-mcp-domains/tls-observation`
(a dotted internal name must be listed in `MCP_PUBLIC_DOMAINS_APP_HOSTS`).
Under any other Host the answer is the Host guard's `404` with a JSON body, and
no report event is logged. The deployment provides a transport that
keeps the bearer confidential between the reporter and the API (the private
network of one host, or TLS between hosts). The route answers with an empty
body, and checks in this order:

| Answer | When |
|---|---|
| `404` | not configured: `MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN` empty, or `MCP_PUBLIC_DOMAINS_ENABLED` or `MCP_PUBLIC_ENDPOINTS_ENABLED` off |
| `401` | no `Authorization` header, more than one, or another credential |
| `413` | a body over 4,096 bytes |
| `422` | an invalid body (a missing or unknown key, a status outside the five, a time that is not an RFC 3339 string with an offset (a number or a numeric string included) or is outside the representable range, a window longer than five minutes, `valid` without a future certificate end, `expired` without an elapsed one) |
| `404` | the binding: no claim with this id, a claim that does not hold its name (`pending` or `released`), another host name or endpoint, or a report observed before the claim was created |
| `409` | a report observed in the future or already past its `valid_until` at receipt; an older report than the stored one; another report observed at the same instant as the stored one |
| `204` | stored, or the stored report sent again unchanged |

Nothing of a request without the credential is read, and no statement runs
before the binding is read. Refusals are logged as
`public_mcp_domain_tls_report_refused` with a reason and the domain id when it
is known: `disabled` (at debug level, so an installation that does not use
reports writes no warning), `credential`, `size`, `invalid`, `binding`,
`future` (observed after the server's clock), `expired` (already past its
`valid_until` at receipt) or `older` (not newer than the stored report). A
stored report is logged as `public_mcp_domain_tls_report_stored` with the
domain id and the status. No log line carries the credential or a host name.

```json
{
  "domain_id": "8d0f6c1e-2c4b-4c47-9f39-3a3a1c1f2b10",
  "hostname": "mcp.example.org",
  "endpoint_id": "e0f1a2b3-c4d5-4e6f-8a7b-9c0d1e2f3a4b",
  "tls": {
    "status": "renewal_failed",
    "observed_at": "2026-11-19T12:00:00Z",
    "valid_until": "2026-11-19T12:05:00Z",
    "certificate_not_after": "2026-11-23T12:00:00Z"
  }
}
```

**Statuses.** `pending` (no certificate yet), `valid` (a certificate whose end
is `certificate_not_after`), `issuance_failed`, `renewal_failed` (shown as such
even while the previous certificate is still valid) and `expired`. All four
`tls` keys are always sent; `certificate_not_after` is `null` when unknown.
Times carry an offset; KMC stores and shows them in UTC.

**The binding.** A report names the claim's `domain_id`, its canonical
`hostname` and its `endpoint_id`, and is accepted only for a claim in state
`active` or `suspended`: a `pending` claim holds nothing, so no certificate can
be its own. The reporter reads that tuple from the operator command, which runs
where the API runs: `python -m src.cli.public_mcp_domains show --hostname <name>`
lists every row of the name with its `domain_id`, `state`, `endpoint_id`,
`created_at` and `check_seq`. Read it right before the probe and again after
it, and skip the report when the row changed. A successful permission call alone
is never evidence of a certificate.

**Freshness and retries.** `valid_until` is at most five minutes after
`observed_at`. Refresh before the window ends; stop when the certificate state
cannot be observed, and the report then expires on its own. Retry a lost
acknowledgement with the same report (`204`); never move its times forward to
pass a check. A report from the future is refused: keep the reporter's clock
synchronised.

**What the observation shows.** The stored report of the claim the
observation shows, while that row holds its name, names it exactly, was
observed at or after the row's `created_at` and not after the observation's own
`observed_at`, and has more than 5 seconds left before its `valid_until`. A
`valid` report whose `certificate_not_after` is not more than 5 seconds ahead is
shown as `expired`; `valid` without `certificate_not_after` and everything that
is not provably current (no report, the feature not configured, another row, a
pending or released row, a report past or within 5 seconds of its
`valid_until`) are `{"status":"unreported"}`. A shown report shortens the
observation's own `valid_until` to the report's, and a `valid` one also to
`certificate_not_after`, when that is sooner; the 5-second margin keeps every
such deadline ahead when the answer arrives. The `tls` object has exactly the keys above; `schema_version` and the
capabilities are unchanged.

**Reassignment.** Evidence is the claim's: a removed or released claim shows
nothing, and a new claim of the same name is a new row that starts
`unreported` until a report names it and was observed after it was created.

**Rotation.** One credential at a time: pause reports, replace
`MCP_PUBLIC_DOMAINS_TLS_REPORTER_TOKEN` on the API and on the reporter, restart
both colours, then send a fresh report. Reports stored before stay valid until
their own `valid_until`.

**Restarts.** Stored reports survive an API restart; a report whose
`valid_until` has passed is `unreported`, whether or not it was deleted yet.

**Data and migration.** Reports live in `public_mcp_domain_tls_reports`, one
row per claim id: the host name and endpoint id the report named, the status and
four times. No person column and no foreign key; never exported, nothing in it
for account erasure. The domain maintenance pass deletes reports whose
`valid_until` has passed, only while reporting is configured; after the
credential is emptied the remaining rows are neither shown nor deleted (clear
them with `DELETE FROM public_mcp_domain_tls_reports;`). The table is created by
the revision `f58_2941_domain_tls_reports`, which locks no existing table.
While the feature is not configured nothing reads or writes the table, so the
code runs before the revision is applied. Set the credential only after the
revision is applied, and empty it before downgrading the revision: with the
credential set and the table missing, the report route and the owner
observation of a live domain answer errors, and the maintenance pass logs
`public_mcp_domain_tls_report_purge_failed` on every run (its domain checks
still run).
