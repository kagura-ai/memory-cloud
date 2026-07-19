# Connector Ingest Contract (ai-worker → memory-cloud)

This document is the **written contract** that an external worker (e.g.
[`kagura-chat-bridge`](https://github.com/kagura-ai/kagura-chat-bridge))
follows when it pushes chat-connector messages (Slack / Discord / Teams) into
Kagura Memory Cloud through the **existing Resource Foundation** ingest path.

There is **no new ingest API** for connectors. Connectors reuse the same
Resource Foundation ingest endpoints that all other resource sources use —
`POST /api/v1/resources/{resource_id}/events` for a single event and
`POST /api/v1/resources/{resource_id}/events/batch` for a batch — exposed by the
`ResourceClient.ingest_event` / `ingest_events` SDK wrappers respectively. This
doc only pins down the connector-specific rules layered on top of those endpoints.

> **Scope.** This is the F6-c (#852) guidance deliverable of epic #755. The
> worker-side write-path decision and its e2e/smoke verification are tracked in
> [ai-worker#91](https://github.com/kagura-ai/kagura-chat-bridge/issues/91),
> not here.

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
  bypasses the `max_resource_tokens` gate but is bounded by the `max_connectors`
  seat cap (#850/#851), not the Pro+ resource-token gate.
- **`delete` must carry `payload: null`.** An `upsert` payload on a `delete` op is
  rejected by request validation.
- **Slug, not UUID.** Address the resource by its slug in the URL; the server
  resolves it against the authenticated workspace.

## Worker config vend: the Locale contract (#1377)

The config vend (`GET /api/v1/workers/config`) types
`WorkerConnectorConfig.locale` as `"en" | "ja" | null`, mirroring the bridge's
`WorkerConfigResponse.locale` (`Literal["en", "ja"]` in kagura-bridge
`models.py::Locale`). A vended value outside that enum fails the bridge's
validation of the **whole** config body and the tenant fails closed
(`config_unavailable`), so both sides of the contract are pinned:

- **memory-cloud** — single source `models.worker_runtime.WORKER_LOCALES`
  (write boundary rejects/normalizes at connector create/update; vend boundary
  degrades non-conforming legacy rows to `null` = worker default).
- **kagura-bridge** — `models.py::Locale`.

Widening the enum (e.g. adding `ko`) requires a coordinated change to **both**
repos: bridge `Locale` first (workers tolerate `null`), then `WORKER_LOCALES`.

## Related

- Epic #755 (F6) — chat ingest reuses Resource Foundation
- #852 (F6-c) — this guidance doc + Slack ingest e2e (e2e tracked in ai-worker#91)
- #853 (F6-d) — PII guardrail + Discord/Teams smoke (ai-worker#91); see [`docs/pii-guardrail-consumption-contract.md`](pii-guardrail-consumption-contract.md) for the guardrail the worker applies before this ingest path
- [`docs/resource-tokens-guide.md`](resource-tokens-guide.md) — general resource-token ingest
- [`docs/resource-foundation-migration.md`](resource-foundation-migration.md) — `resource_pk` / UUID model background
