# Public MCP usage events

A resource published as an anonymous, read-only MCP endpoint (`/mcp/public/<public_id>`, see [Resource Tokens Guide › Publishing a resource as a public MCP endpoint](resource-tokens-guide.md#publishing-a-resource-as-a-public-mcp-endpoint)) records one **usage event** per completed `tools/call` ([#2678](https://github.com/kagura-ai/memory-cloud/issues/2678)). The events tell the workspace owner — or a self-hosted dashboard reading the stream — which public tools were used, how they went, how long they took and which documents were returned, without storing anything a visitor typed or anything that identifies a visitor.

Operator settings: [Deployment › Public MCP usage events](deployment.md#public-mcp-usage-events--issue-2678). Owner health route: [API Reference › Public MCP endpoint publication](api-reference.md#public-mcp-endpoint-publication).

## What is recorded

Exactly one event per completed outer public `tools/call`, on both protocol eras (session and stateless). Nothing is recorded for `initialize`, `tools/list`, `server/discover`, `ping`, notifications, or for a handler calling another handler internally. Refused calls (an unknown tool name, a bad argument, the rate limit) are recorded too, with their outcome.

### Schema v1

Every event and every page carries `schema_version: 1`. Representative events for all five tools, the refusals, a gap and a full page are in [`backend/tests/fixtures/public_mcp_usage_events_v1.json`](../backend/tests/fixtures/public_mcp_usage_events_v1.json); a test validates them against the export models.

| Field | Type | Meaning |
|---|---|---|
| `event_id` | UUID | Immutable id of the event; the id a consumer stores. |
| `kind` | `tool_call` \| `gap` | A gap row reports events the server could not record (below). |
| `occurred_at` | UTC timestamp (`Z`) | Start of the call; for a gap, when the gap row was written. |
| `endpoint_id`, `workspace_id`, `context_id`, `resource_pk` | UUID | Resolved by the server from the published endpoint — never from the request. An unpublish and re-publish is a new `endpoint_id`. |
| `resource_id` | string | The resource slug at the time of the call (a slug can be reused later; `resource_pk` is the identity). |
| `tool` | `recall` \| `reference` \| `explore` \| `list_tags` \| `get_context_info` \| `other` | The requested tool when it is one of the five public tools; any other requested name is stored as `other`, never as typed. |
| `outcome` | `success` \| `degraded` \| `denied` \| `rate_limited` \| `invalid_request` \| `error` \| `cancelled` | How the call ended. The HTTP status of a tool call is always 200; this field is what classifies it. |
| `error_code` | string \| null | The public error code of a refusal or failure (`unknown_tool`, `invalid_argument`, `rate_limit_exceeded`, `context_not_available`, `memory_not_found`, `temporarily_unavailable`, `timeout`, …); `null` on `success`, `degraded` and `cancelled`. |
| `era` | `session` \| `stateless` | Protocol era of the call. |
| `protocol_version` | string \| null | The negotiated or declared MCP version (`YYYY-MM-DD`); `null` when unknown. |
| `duration_ms` | integer ≥ 0 | Wall time of the whole public pipeline; for `cancelled`, the time until the cancellation. |
| `result_count` | integer \| null | `recall`: results returned; `explore`: related entries; `list_tags`: tags; `reference`: 1 (0 when not found). `null` for `get_context_info` and for every refusal or failure — an unknown value is `null`, never 0. |
| `continuation` | boolean | `reference` with `content_offset > 0` (a later page of the same entry), so initial reads and continuations count separately. |
| `documents` | array \| null | Up to 25 `{"doc_key", "chunks", "rank"}`: the documents of the returned rows, deduplicated — several chunks of one document are one entry with `chunks` = how many rows it had and `rank` = its best position. `null` when the tool returns no documents or the call was refused. |
| `documents_truncated` | boolean | More than 25 distinct documents were returned. |
| `session_key` | 32 hex \| null | Only with session details on (below). |
| `client_family` | string \| null | Only with session details on (below). |
| gap rows: `dropped_count`, `gap_started_at`, `gap_ended_at`, `gap_reason` | | How many events were lost between the two times, and why: `queue_full`, `write_failed` or `shutdown`. Gap rows carry no workspace and appear in every scope. |

A `doc_key` is an opaque 32-hex key: an HMAC of the resource identity and the resource's own document id under the deployment's `AUDIT_HMAC_KEY`. The same document keeps its key across re-publishes of the resource; the same document id under another resource has another key. A guessable document id cannot be recovered from an export. The server keeps the key → document id map for the resource (deleted with the resource) so that owner-side tools can resolve a key without the HMAC.

**Timeout and cancellation.** A tool that runs past its timeout answers the public error result and is recorded as `error` with `error_code: "timeout"`. A call cancelled because the client went away or the server shut down is recorded as `cancelled` and then cancelled. A call refused because the rate-limit store cannot be read is `error` / `temporarily_unavailable`.

### Never recorded

No query text, no tool arguments, no result text, no memory ids, no document titles, summaries, URLs or document ids, no raw session id, no IP address, no user agent, no request headers, no workspace, context or resource names, and no credentials. The public endpoint has no credentials to record.

### Session details (off by default)

With `MCP_PUBLIC_TELEMETRY_SESSION_DETAILS_ENABLED=true`, session-era events also carry:

- `session_key` — an HMAC of the UTC date, the endpoint and the server-minted session id under `AUDIT_HMAC_KEY` (validated at startup in production). It is stable within one UTC day and changes the next day; the raw session id is never stored. **Session counts are connections, not people:** one person can open many sessions, and one session can serve many people behind one client.
- `client_family` — the name the client declared in `initialize` (`clientInfo.name`), lower-cased and reduced to `[a-z0-9._-]`, at most 32 characters. It is **unverified**: any client can declare any name. Every export page says so with `"client_family_verified": false`.

Stateless calls have neither. With the setting off (the default), both are always `null` and nothing reads `clientInfo`.

## How events are written, and what can be lost

A public call only puts its event on a bounded in-memory queue (`MCP_PUBLIC_TELEMETRY_QUEUE_SIZE`, default 2,000 per API process); it never waits on the database. A background writer in each API process writes the queue in batches (up to 200 events, or every `MCP_PUBLIC_TELEMETRY_FLUSH_INTERVAL_SECONDS`) in one transaction bounded by `MCP_PUBLIC_TELEMETRY_WRITE_TIMEOUT_SECONDS`. A failed batch is retried once and then dropped.

Every dropped event is counted and written as a **gap row** at the head of the next successful batch:

| `gap_reason` | When |
|---|---|
| `queue_full` | The queue was full (the database is slower than the traffic). |
| `write_failed` | A batch failed twice (database unavailable, write timeout). |
| `shutdown` | The process stopped and the database did not answer within the shutdown budget. |

So the stream says where it is incomplete instead of skipping silently. If the database does not answer during shutdown either, the count is logged (`public_mcp_telemetry_lost_at_shutdown`). A process crash loses at most the events in its queue. Search keeps working while the database is slow or down; only the telemetry is lost, and a failure inside the telemetry itself is logged once (`public_mcp_telemetry_failed`) and never changes a tool result.

## Retention

A daily job (04:50 UTC, on the scheduler leader) deletes events older than `MCP_PUBLIC_TELEMETRY_RETENTION_DAYS` (default 90), gap rows included, and the document keys of resources that have had no live public endpoint since before that cutoff.

## Reading the stream: the commit-safe cursor

Events are read in pages in `(txid, id)` order: `txid` is the database transaction that wrote the event, `id` its storage order. A page only contains events whose transaction is older than every transaction still running on the database server when the page is read. Every such transaction has finished, so that set of events can no longer change, and a cursor that has passed it can never miss an event that commits later: a later commit always sorts after the cursor. A cursor that only followed insertion ids would skip an event whose transaction took an id first and committed last.

- A cursor is opaque. An omitted cursor starts at the oldest retained event; `next_cursor` is always returned (the same cursor when the page is empty), and replaying a cursor returns the same events plus any sealed since.
- A long-running write transaction anywhere on the database server (a migration, a long maintenance run) delays publication until it ends: events are late, never lost. A page reports `coverage.unsealed_events`, the number of committed events not yet published, so "nothing happened" and "not published yet" can be told apart.
- **`cursor_expired` (410).** Retention advances a purge mark to the last deleted `(txid, id)`. A cursor below that mark may have missed purged events; it is refused with `error: "cursor_expired"` and `details.oldest_cursor`, where the reader restarts knowing there is a hole. A malformed cursor is `400` `cursor_invalid`.

A page envelope: `{schema_version, deployment_id, as_of, events, next_cursor, has_more, coverage: {gaps_in_page, dropped_in_page, unsealed_events, oldest_retained_at, retention_days, writer_enabled}, client_family_verified: false}`. `deployment_id` is the `DEPLOYMENT_ID` setting.

## Health

`GET /api/v1/resources/{resource_id}/public-mcp/usage-health` (workspace owner) returns the live endpoint's events of the last 24 hours by outcome, the deployment's gap rows in that window, and the writer state of the API process that answered (`writer.process_local: true`: with several API processes, each has its own queue). The gap rows are the persistent view of loss; the writer numbers are a snapshot of one process.
