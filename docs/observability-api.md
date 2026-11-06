# Observability API

An internal, read-only HTTP API for a **self-hosted dashboard** that watches
resources: which resources exist and whether they are published, how their
indexer is doing, what was ingested, and how the anonymous public MCP endpoint
([#2675](https://github.com/kagura-ai/memory-cloud/issues/2675)) is used
([#2678](https://github.com/kagura-ai/memory-cloud/issues/2678)). Added in
[#2679](https://github.com/kagura-ai/memory-cloud/issues/2679).

It is read with a dedicated **observation credential**, never with a session or
an owner API key, so the dashboard does not have to hold (or put in a browser)
a credential that could change anything.

- [Enabling it: internal only](#enabling-it-internal-only)
- [Observation credentials](#observation-credentials)
- [Conventions](#conventions)
- [Endpoints](#endpoints)
- [Errors](#errors)
- [Coverage and what the numbers mean](#coverage-and-what-the-numbers-mean)
- [Privacy](#privacy)

## Enabling it: internal only

The API is **off by default**. While `OBSERVABILITY_API_ENABLED=false`, every
`/api/v1/observability/*` route answers `404` with the same body as an unknown
path, before any header is read; a valid credential changes nothing.
Credentials can still be issued while it is off, so an operator can prepare
them first.

| Variable | Default | Effect |
|---|---|---|
| `OBSERVABILITY_API_ENABLED` | `false` | Serve the API. |
| `OBSERVABILITY_API_ALLOWED_CIDRS` | *(empty)* | Comma-separated CIDR ranges allowed to call it (for example `10.0.0.0/8,172.16.0.0/12`). A client outside them gets the same `404` as when the API is off. Empty allows any address; the credential is still required. A malformed range stops the server at startup. |
| `OBSERVABILITY_API_RATE_LIMIT_PER_MINUTE` | `600` | Requests per credential per minute; over it `429` with `Retry-After: 60`. A Redis outage serves the request (the API is internal and authenticated). |
| `OBSERVABILITY_INGESTION_SEAL_SECONDS` | `120` | `10`–`3600`. Ingestion events younger than this are not published yet (see [the time seal](#ingestion-time-seal)). |

**Keep it internal to the deployment.** The address checked by the allow-list
is `request.client.host`. Behind a reverse proxy that is the proxy's address
unless the server runs with trusted proxy headers (`FORWARDED_ALLOW_IPS`), so
also block `/api/v1/observability/` at the proxy for everything but the
dashboard's network. Even internal requests need an observation credential:
there is no anonymous access.

## Observation credentials

A credential is a bearer secret `kagura_obs_` + 43 URL-safe characters, sent as
`Authorization: Bearer kagura_obs_…`. Every credential is bound to **this
deployment** (its `deployment_id` is in `capabilities`) and to **one
workspace, or one resource of that workspace**. The binding comes from the
credential, never from the request.

- **Issued by the workspace owner** (session or owner API key) for the owner's
  current workspace. The secret is in the create (and rotate) response only;
  the server stores its SHA-256 verifier and compares in constant time.
- **Always expires**: `ttl_days` 1–365, default 90.
- **Rotate** to get a new secret with the same binding and the *remaining*
  lifetime; the old secret keeps working for `grace_seconds` (0–86,400, default
  0), so a dashboard can switch without a gap.
- **Revoke** at any time; the next request is a `401`.
- **Refused everywhere else**: the secret is not an API key, resource token,
  share key, OAuth token or session, so ingestion, memory reads and writes,
  owner routes, the credential management routes and `/mcp` all answer `401`.
  A deployment-wide (all workspaces) scope is not offered.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/observation-credentials` | `{name, scope: "workspace" \| "resource", resource_id?, ttl_days?}` → `201` with `secret`. `scope=resource` needs the slug of a resource of the current workspace (`404` otherwise); `resource_id` with `scope=workspace`, or `ttl_days` outside 1–365, is `422`. |
| `GET /api/v1/observation-credentials` | The workspace's credentials, revoked and expired ones included, with `status` `active` / `expired` / `revoked`. Never the secret. |
| `POST /api/v1/observation-credentials/{credential_id}/rotate` | `{grace_seconds?}` → `201` with the new `secret`; `rotated_from` names the old id. `404` for an unknown, foreign, revoked or expired credential, or one already rotated (rotate the newest one). |
| `DELETE /api/v1/observation-credentials/{credential_id}` | Revoke → `204` (again: `204`); `404` for an id of another workspace. |

```bash
curl -X POST "$KAGURA/api/v1/observation-credentials" \
  -H "Authorization: Bearer kagura_{owner_key}" -H "Content-Type: application/json" \
  -d '{"name": "dashboard", "scope": "workspace", "ttl_days": 90}'
# → {"id": "obsk_…", "secret": "kagura_obs_…", "status": "active", "expires_at": "…Z", …}
```

## Conventions

- **Schema versions.** Every response carries `schema_version` (`1`),
  `deployment_id` and `as_of` (UTC server time of the read, `Z` suffix). Every
  `GET` takes `schema_version` (default `1`), `documents/resolve` takes it in
  the body; any other value is `400 unsupported_schema_version` with
  `details.supported: [1]`. `capabilities.schema_versions` lists what each
  endpoint serves.
- **Fixtures.** One JSON per response and per error body under
  [`backend/tests/fixtures/observability_api_v1/`](../backend/tests/fixtures/observability_api_v1/),
  checked against the response models in CI. The `events` fixture is the page
  of [`public_mcp_usage_events_v1.json`](../backend/tests/fixtures/public_mcp_usage_events_v1.json).
- **Pagination.** `events` and `ingestion-events` use opaque keyset cursors
  (base64url JSON; do not parse them). `next_cursor` is always present and
  `has_more` says whether to call again now. On `ingestion-events` an empty
  page hands back the input cursor; on `events` a page without more rows hands
  back a position at the publication horizon, which can move forward even when
  the page is empty (see
  [Public MCP usage events](public-mcp-usage-events.md)). Store `next_cursor` after you processed a page; replaying a cursor
  answers the same rows plus anything published since.
- **Maximum sizes.** `limit` 1–500 (default 100) for `events` and
  `ingestion-events` (`422` outside); `resources` and `snapshots` return at most
  500 resources with `truncated: true` beyond that (no pagination);
  `documents/resolve` takes 1–100 keys.
- **Scope.** A workspace credential sees every resource of its workspace and
  must pass `resource_id` to per-resource reads (`ingestion-events`,
  `documents/resolve`); a resource credential may omit it. A slug outside the
  scope and a slug that does not exist both answer `404 resource_not_found`.

## Endpoints

### GET /api/v1/observability/capabilities

What this credential may read and the limits that apply.

```json
{"schema_version": 1, "deployment_id": "default", "as_of": "…Z",
 "schema_versions": {"resources": [1], "snapshots": [1], "events": [1], "ingestion_events": [1], "documents": [1]},
 "scope": {"kind": "workspace", "workspace_id": "…", "resource_id": null},
 "credential": {"id": "obsk_…", "expires_at": "…Z"},
 "limits": {"events_page_max": 500, "ingestion_page_max": 500, "documents_resolve_max": 100, "resources_max": 500},
 "retention": {"usage_events_days": 90, "ingestion_events": "resource lifetime"},
 "ingestion_seal_seconds": 120, "writer_enabled": true, "server_time": "…Z"}
```

### GET /api/v1/observability/resources

The resources in scope: `resource_pk` (identity; a reused slug gets a new
one), `resource_id` (slug), `name`, `context_id` (the live context, or
`null`), `created_at`, and `publication` — the live public MCP endpoint
`{endpoint_id, public_id, public_name, published_at, updated_at, serving}` or
`null`. `serving` is whether the anonymous endpoint answers right now (operator
switch on, not unpublished, context still public and bound, and no plan pause or
capacity lock answering `503`). No memory counts,
no context descriptions.

### GET /api/v1/observability/snapshots

`?resource_id=` (optional; omitted = every resource in scope). One item per
resource with `indexer` and `publication`:

| `indexer` field | Meaning |
|---|---|
| `state` | `never_run` (no indexer state, or it never completed a run), `idle`, `queued`, `running`, or `error` (the last run failed). `never_run` is not `idle`. |
| `last_run_at`, `next_run_at` | UTC times, or `null`. |
| `seconds_since_last_run` | The **age** of the last run. Not ingest lag. |
| `last_offset` | The last processed ingestion `event_id`. Not a backlog count. |
| `last_run` | Counters of the last run `{applied_upserts, applied_deletes, errors, failed_events, skipped_reason}`; `failed_events` is a count (capped at 20), never the ids or error text. `null` when never run. |

The body repeats the two warnings in `field_notes`.

### GET /api/v1/observability/events

`?cursor=&limit=100&resource_id=&schema_version=1`. The public MCP usage
events in scope (one per completed anonymous `tools/call`), in the schema and
envelope of [Public MCP usage events](public-mcp-usage-events.md). Gap rows
(telemetry that was lost) belong to every scope. Ordering is commit-safe: a
row is published only once every transaction that could still insert before
it has finished, so a late commit, a page replay or a dashboard restart from a
stored cursor never skips a row; rows waiting for that are counted in
`coverage.unsealed_events`. A cursor older than the retention purge is `410
cursor_expired` with `details.oldest_cursor`: continue from there, knowing
there is a hole, or start again without a cursor.

### GET /api/v1/observability/ingestion-events

`?resource_id=&cursor=&limit=100&schema_version=1`. A payload-free summary of
one resource's ingestion events, oldest first:

```json
{"resource_id": "product-catalog",
 "events": [{"event_id": "1841", "received_at": "…Z", "op": "upsert",
             "doc_key": "3f9a0c1e5b7d2468ace0bdf13579bdf1",
             "processing": {"status": "processed", "failed": false}}],
 "next_cursor": "…", "has_more": false,
 "coverage": {"sealed_before": "…Z", "seal_seconds": 120, "retention": "resource lifetime", "oldest_event_id": "17"}}
```

- `event_id` is stable; `received_at` is when the event was accepted.
- `processing.status`: `processed` once the indexer's offset reached the
  event, `pending` before, `unknown` when the resource has no indexer state.
  `failed: true` marks an event the indexer recorded as failed (it is still
  `processed`: the indexer moved past it).
- <a id="ingestion-time-seal"></a>**Time seal.** Only events received at least
  `OBSERVABILITY_INGESTION_SEAL_SECONDS` ago are published
  (`coverage.sealed_before`). Event ids are assigned at insert and
  transactions commit later, so a young event with a smaller id could still
  appear; the seal makes the ascending cursor safe for any ingest transaction
  shorter than it (ingestion writes one short request transaction). To
  re-synchronise, omit the cursor.
- **Retention**: ingestion events live as long as their resource; this is
  independent of the usage events' retention. A cursor below the resource's
  oldest remaining event is `410 cursor_expired` with `details.oldest_cursor`.
  A cursor from a deleted resource whose slug was reused gets the same answer.
- Never the payload, the document id, the version, the idempotency key or the
  event metadata.

### POST /api/v1/observability/documents/resolve

```json
{"resource_id": "product-catalog", "doc_keys": ["a3f19c0b7e2d4c5a8b6e1f0d9c8b7a65"], "schema_version": 1}
```

→ `{"resource_id", "documents": {doc_key: {"title", "source_url"} | null}, "publication_serving"}`.
Resolves the opaque `doc_key`s of usage and ingestion events to what the
public endpoint already shows: `title` (the document's summary, cut to 200
characters, as anonymous `recall` returns it) and `source_url` (only an
http(s) URL, else `null`). A key resolves only while the resource's
publication **serves right now**; otherwise every key is `null` and
`publication_serving` is `false` — unpublished, operator switch off, context no
longer public. Unknown keys, keys of another resource, deleted documents, and
keys from before a slug was reused also answer `null`. 1–100 keys of 32
lowercase hex characters (`422` otherwise). A workspace whose plan pauses
public serving, or that is over its capacity, answers `503
temporarily_unavailable` with `Retry-After: 300`.

### doc_key

`doc_key` is an HMAC of the resource identity and the resource's own document
id, keyed with the deployment's `AUDIT_HMAC_KEY`, cut to 32 hex characters. It
is the same in usage events and ingestion events, so the two join on it; it
does not reveal the document id and changes when the resource is recreated
(even under the same slug). Rotating `AUDIT_HMAC_KEY` changes future keys;
`documents/resolve` keeps resolving keys it has seen.

## Errors

Every error body is `{"error", "message", "details"}`:

| Status | `error` | When |
|---|---|---|
| `400` | `unsupported_schema_version` | `schema_version` other than 1; `details.supported: [1]`. |
| `400` | `cursor_invalid` | A cursor this API did not issue. |
| `401` | `AUTH-001` | No bearer, not an observation credential, unknown, revoked or expired (one message for all). |
| `404` | `HTTP-404` | API switched off, or the client address is outside `OBSERVABILITY_API_ALLOWED_CIDRS`. |
| `404` | `resource_not_found` | `resource_id` outside the credential's scope or nonexistent. |
| `410` | `cursor_expired` | The cursor is older than the retained events; `details.oldest_cursor`. |
| `422` | `VAL-001` | `limit` outside 1–500, too many or malformed doc keys, missing `resource_id` with a workspace credential. |
| `429` | `RATE-001` | Per-credential minute budget spent; `Retry-After: 60`. |
| `503` | `temporarily_unavailable` | `documents/resolve` on a paused or capacity-locked workspace; `Retry-After: 300`. |

## Coverage and what the numbers mean

- **Age is not lag; offset is not backlog.** `seconds_since_last_run` is the
  time since the indexer last ran, not how far behind ingestion it is;
  `last_offset` is an event id, not a count of pending events. To see what is
  pending, page `ingestion-events` and count `processing.status: "pending"`.
- **Usage events**: `coverage.unsealed_events` > 0 means rows exist that are
  not published yet (a long write transaction elsewhere in the database holds
  them back; they are late, not lost). Gap rows and `coverage.gaps_in_page` /
  `dropped_in_page` report telemetry that was lost (queue full, write failure,
  shutdown). `coverage.writer_enabled: false` means this deployment does not
  record usage events.
- **Ingestion events**: `coverage.sealed_before` is the newest receipt time a
  page can contain.

## Privacy

The API never returns ingestion payloads, document ids, versions, idempotency
keys, event metadata, memory ids, memory text other than the public title of
`documents/resolve`, indexer error messages or failed event ids, query text,
IP addresses or user agents. Usage events carry no session identifiers unless
the operator turned session details on (see
[Public MCP usage events](public-mcp-usage-events.md)); session counts are
connections, not people.
