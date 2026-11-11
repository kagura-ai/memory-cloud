# Connector Ingest Contract (ai-worker → memory-cloud)

This document is the **written contract** that an external connector worker follows when it pushes chat-connector messages (Slack / Discord / Teams) into
Kagura Memory Cloud through the **existing Resource Foundation** ingest path.

There is **no new ingest API** for connectors. Connectors reuse the same
Resource Foundation ingest endpoints that all other resource sources use —
`POST /api/v1/resources/{resource_id}/events` for a single event and
`POST /api/v1/resources/{resource_id}/events/batch` for a batch — exposed by the
`ResourceClient.ingest_event` / `ingest_events` SDK wrappers respectively. This
doc only pins down the connector-specific rules layered on top of those endpoints.

> **Scope.** This is the F6-c (#852) guidance deliverable of epic #755. The
> worker-side write-path decision and its e2e/smoke verification are tracked on
> the consumer side, not here.

## Prerequisites (already shipped)

| Piece | Issue | What it gives the worker |
|---|---|---|
| `workspace_connectors` schema + ORM + `max_connectors` tier | #850 | A connector profile row, 1:1 with a `resources` row (`resource_pk` FK) |
| Connector setup flow + seat-cap + connector-scoped token mint | #851 | A **connector-scoped resource token** to authenticate ingest |
| Resource Foundation `/events` endpoint + SDK | (pre-existing) | `ResourceClient.ingest_event` / `ingest_events` |

The worker does **not** create tables, mint its own tokens, or set internal
primary keys. It is handed a connector-scoped token and ingests against it.

## Authentication

Send the connector-scoped resource token in the **`X-Resource-API-Key`** header:

```
POST /api/v1/resources/{resource_id}/events
X-Resource-API-Key: <connector-scoped resource token>
Content-Type: application/json
```

The `ResourceClient` SDK sets this header for you when constructed with the
connector token. `{resource_id}` is the human-readable slug of the connector's
resource (e.g. `slack-acme-eng`); the server resolves slug + authenticated
workspace → internal UUID. The worker never sees or sends the UUID.

## Event payload

Single event (`ResourceClient.ingest_event`):

```json
{
  "op": "upsert",
  "doc_id": "1716240000.001500",
  "version": 1,
  "payload": { "text": "...", "channel": "C123", "user": "U456" },
  "idempotency_key": "<connector_id>:1716240000.001500"
}
```

| Field | Rule |
|---|---|
| `op` | `"upsert"` or `"delete"` (exact). |
| `doc_id` | Stable ID across versions, 1–255 chars. **Map to the Slack/Discord/Teams message ts / message ID.** |
| `version` | Integer ≥ 1, or `null`. **Map to the message edit revision.** A newer `version` for the same `doc_id` supersedes older ones (old versions are cleaned up automatically). `null` on a `delete` means delete-all-versions. |
| `payload` | JSON object for `upsert`; **must be `null` for `delete`**. Projected into searchable text by the resource's schema. |
| `idempotency_key` | Optional in general, **mandatory and prefixed for connectors** — see below. |

### Batch ingest

`ResourceClient.ingest_events` posts an array of up to **100** events to the
batch endpoint `POST /api/v1/resources/{resource_id}/events/batch`:

```json
{
  "events": [
    {
      "op": "upsert",
      "doc_id": "1716240000.001500",
      "version": 1,
      "payload": { "text": "edited message", "channel": "C123", "user": "U456" },
      "idempotency_key": "<connector_id>:1716240000.001500"
    },
    {
      "op": "delete",
      "doc_id": "1716240000.001499",
      "version": null,
      "payload": null,
      "idempotency_key": "<connector_id>:1716240000.001499"
    }
  ]
}
```

The server rejects batches of **more than 100** events (`max_length=100`).
Chunk larger backfills into ≤100-event calls.

## Idempotency contract (connector-specific)

`resource_events.idempotency_key` is **globally UNIQUE**. To keep connector
namespaces from colliding, every connector event's `idempotency_key` **must be
prefixed with `{connector_id}:`** — where `{connector_id}` is the
`workspace_connectors.id` UUID for this connector:

```
idempotency_key = f"{connector_id}:{message_id}"
```

The server enforces this prefix (`validate_connector_idempotency_key`): a
connector-owned event whose key is missing or not prefixed with
`"{connector_id}:"` is rejected with a `ValidationError` on `idempotency_key`.
The suffix after the colon is the worker's choice — the connector-local message
identifier (e.g. Slack `ts`) is the natural value, which also makes retries of
the same message naturally idempotent.

## End-to-end flow

```
worker (connector token)
  └─ ResourceClient.ingest_event(op=upsert, doc_id=<msg ts>, version=<edit rev>,
                                 payload={...}, idempotency_key="<connector_id>:<msg ts>")
       └─ POST /api/v1/resources/{slug}/events  (X-Resource-API-Key)
            └─ resource_events row written (resource_pk derived from the token)
                 └─ indexer picks up the row → Qdrant + Memory projection
                      └─ recall / explore / reference return the message content
```

## Known pitfalls

- **`resource_pk` is server-derived — do not send it.** The writer binds
  `resource_pk` from the authenticating token (`token_record.resource_pk`), and a
  `before_insert` invariant (#323/#390) raises `IntegrityError` if a row reaches
  the DB without `resource_pk` set alongside `resource_id`. From the worker's
  side this is automatic: authenticate with the connector-scoped token and the
  server fills `resource_pk`. There is no worker-visible field for it.
- **Quota is counted per connector resource.** The hourly quota counter is keyed
  by `resource_id` (the resource slug) + `workspace_id`
  (`resource_quota_service._build_key`, #328/#332) and charges `len(events)` per
  call, so batching does **not** bypass the ceiling. A connector minting a token
  bypasses the `max_resource_tokens` cap but is bounded by the `max_connectors`
  seat cap (#850/#851), not the resource-token cap. Creating a connector at all
  requires a plan with the `connectors` feature — XL by default (#1551); the seat
  cap is the second gate. This gates **new creation only**: connectors that
  already exist on a lower plan keep ingesting, and `max_connectors` on those
  plans bounds only what they already hold.
- **`delete` must carry `payload: null`.** An `upsert` payload on a `delete` op is
  rejected by request validation.
- **Slug, not UUID.** Address the resource by its slug in the URL; the server
  resolves it against the authenticated workspace.

## Worker config vend: the Locale contract (#1377)

The config vend (`GET /api/v1/workers/config`) types
`WorkerConnectorConfig.locale` as `"en" | "ja" | null`, mirroring the bridge's
`WorkerConfigResponse.locale` (`Literal["en", "ja"]` in the connector worker
`models.py::Locale`). A vended value outside that enum fails the bridge's
validation of the **whole** config body and the tenant fails closed
(`config_unavailable`), so both sides of the contract are pinned:

- **memory-cloud** — single source `models.worker_runtime.WORKER_LOCALES`
  (write boundary rejects/normalizes at connector create/update; vend boundary
  degrades non-conforming legacy rows to `null` = worker default).
- **connector worker** — `models.py::Locale`.

Widening the enum (e.g. adding `ko`) requires a coordinated change to **both**
repos: bridge `Locale` first (workers tolerate `null`), then `WORKER_LOCALES`.

## Worker config vend: per-channel destinations (#1759)

A connector writes every channel into its default context (`context_id`)
unless an admin gives a channel a destination of its own. This section is the
contract the connector worker implements for that.

**Capability handshake.** The worker declares that it implements this section
with `GET /api/v1/workers/config?...&capabilities=channel_routes`
(comma-separated tokens; unknown tokens are ignored). Then, and only then, the
body additionally carries, always (an empty list when the connector uses no
routing):

```json
{
  "context_id": "<default context>",
  "channel_routes": [
    {"channel_id": "C0SALES", "action": "route", "context_id": "<context>"},
    {"channel_id": "C0HR", "action": "skip"}
  ],
  "unrouted_private_channels": "skip"
}
```

A connector **uses routing** when it has at least one route or its
`unrouted_private_channels` is `skip`. Such a connector is **not ready** (the
usual `404 WORKER-CONNECTOR-001`, decided before the conditional GET) for a
request without the capability: a worker that does not know this contract
would write every channel, private ones included, into the default context.
For a request without the capability and a connector that uses no routing, the
body and `config_revision` are exactly what they were before this feature.

**What the worker does for a message in channel C:**

| `channel_routes` entry for C | Ingest | Answer from memory in C |
|---|---|---|
| `action: "route"` | write to that entry's `context_id` | channel-scoped recall reads from that `context_id` |
| `action: "skip"` | do not ingest | do not answer from memory |
| any other `action` | treat as `skip` | treat as `skip` |
| no entry, C is private, `unrouted_private_channels: "skip"` | do not ingest | do not answer from memory |
| no entry otherwise | write to the default `context_id` | as before |

- A worker that cannot tell whether a channel is private treats it as private.
- A channel with an entry never falls back to the default `context_id`. The
  server vends a route whose destination was deleted (or soft-deleted, or is
  no longer in the connector's workspace) as `skip`, so the channel stops being
  ingested until an admin picks a new destination; it never reappears in the
  default context.
- The writes go through the `kmc` block's key, which acts as one user (the
  key's owner). The server accepted each route only after checking that this
  user may write the destination; it does not change the key's scope.
- The resource-token ingest path (`resource` block) has no per-channel
  destination: a channel with an entry, and a private channel under
  `unrouted_private_channels: "skip"`, never goes through it.
- `config_revision` (and the ETag) covers the declared capability and a
  digest of the effective routes and the switch, so a destination deleted
  later changes the revision although `config_version` did not move: the
  worker refetches and sees the `skip`.

**Rollout order for operators** (also in `docs/deployment.md`): upgrade the
worker to one that declares `capabilities=channel_routes`, then set
`CONNECTOR_CHANNEL_ROUTING_ENABLED=true`. With the flag off, the settings PATCH
refuses to create or change a route or to set the switch to `skip` (clearing is
always allowed). Stored routes are honoured, and kept from a worker without the
capability, whatever the flag says.

## Related

- Epic #755 (F6) — chat ingest reuses Resource Foundation
- #852 (F6-c) — this guidance doc + Slack ingest e2e (e2e tracked in ai-worker#91)
- #853 (F6-d) — PII guardrail + Discord/Teams smoke (ai-worker#91); see [`docs/pii-guardrail-consumption-contract.md`](pii-guardrail-consumption-contract.md) for the guardrail the worker applies before this ingest path
- [`docs/resource-tokens-guide.md`](resource-tokens-guide.md) — general resource-token ingest
- [`docs/resource-foundation-migration.md`](resource-foundation-migration.md) — `resource_pk` / UUID model background
