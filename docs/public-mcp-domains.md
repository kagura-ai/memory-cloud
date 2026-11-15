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
`serve_until` after the database clock. Both return the same serving endpoint,
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
`Cache-Control: no-store` (refusals included). The workspace comes from the
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
| `serving` | whether the alias answers right now (the alias statement resolves this domain) |
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
| `429` | `RATE-001` (`Retry-After` header, `details.retry_after`, `details.limit`) | over 20 verify calls per hour or 6 activation attempts per day |
| `503` | `public_mcp_domain_verification_unavailable` | the lookup was indeterminate, or a counter could not be read |

A request body that fails validation (no `hostname`, or one over 255
characters) gets the generic request-validation `422`, without
`Cache-Control: no-store`.

### Capability

`GET /api/v1/system/info` reports `features.public_mcp_domains`: `true` while
`MCP_PUBLIC_DOMAINS_ENABLED` is on. It says only that the deployment offers
the feature, never which hosts it uses.

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
  deletes the row instead (also a `released` one still in its cooldown), so
  the name is free for every workspace at once.
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
| `MCP_PUBLIC_DOMAINS_APP_HOSTS` | *(empty)* | Further names the application is reached under, comma-separated: `name` = that host, `.name` = that host and every name under it. Reserved for claims. |
| `MCP_PUBLIC_DOMAINS_RESERVED_SUFFIXES` | *(empty)* | Names never claimable, nor any name under them, comma-separated (no leading dot). Refused on the request path from the first request after a restart, whatever the domain's row says; the next check suspends the domain (`reserved`). |
| `MCP_PUBLIC_DOMAINS_REASSIGN_COOLDOWN_HOURS` | `168` | Hours a released name stays closed to other workspaces (0–2160). |
| `MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE` | `3` | Domains (`pending`, `active`, `suspended`) per workspace (1–50). |

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

1. **Run the migration first.** `alembic upgrade head` creates
   `f50_2809_public_mcp_domains`: one new, empty table and its indexes. It has
   no foreign key and no statement on any existing table, so it locks no
   existing table. Run it before `MCP_PUBLIC_DOMAINS_ENABLED=true`.
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
binding with it.

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
the row only and is deleted with it (a `pending` row after 7 days or on
remove, a `released` row when its cooldown ends). The table is operational
data: never exported, nothing in it for account erasure.

- Workspace soft delete: aliases answer `404` at once (see the right above).
- Workspace hard delete: the rows stay (no foreign key); the job releases held
  rows with the cooldown and deletes claims, then deletes the released rows
  when the cooldown ends.
- Unpublish: released in the same transaction (above).

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
ended); `suspended` carries the suspension reason.

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

No log line, usage event, job count or audit row carries a host name, a token,
a DNS answer, a resolved address or the `Origin` of an alias request.
