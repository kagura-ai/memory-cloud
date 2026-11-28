# Public MCP usage events

A resource published as an anonymous, read-only MCP endpoint (`/mcp/public/<public_id>`, see [Resource Tokens Guide › Publishing a resource as a public MCP endpoint](resource-tokens-guide.md#publishing-a-resource-as-a-public-mcp-endpoint)) records one **usage event** per completed `tools/call` ([#2678](https://github.com/kagura-ai/memory-cloud/issues/2678)). The events tell the workspace owner — or a self-hosted dashboard reading the stream — which public tools were used, how they went, how long they took and which documents were returned. Usage events never carry anything a visitor typed or anything that identifies a visitor; everything else a public call leaves on the server is listed in [What a public call leaves on the server](#what-a-public-call-leaves-on-the-server).

Operator settings: [Deployment › Public MCP usage events](deployment.md#public-mcp-usage-events--issue-2678). Owner health and statistics routes: [API Reference › Public MCP endpoint publication](api-reference.md#public-mcp-endpoint-publication).

## What is recorded

Exactly one event per completed outer public `tools/call`, on both protocol eras (session and stateless). Nothing is recorded for `initialize`, `tools/list`, `server/discover`, `ping`, notifications, or for a handler calling another handler internally. Refused calls (an unknown tool name, a bad argument, the rate limit) are recorded too, with their outcome.

**REST public searches made with a bound key ([#3035](https://github.com/kagura-ai/memory-cloud/issues/3035)).** While the operator switch `PUBLIC_SEARCH_TELEMETRY_ENABLED` is on, a REST public search (`POST /api/v1/public/{context_id}/search`) made with an API key bound to the context is recorded too, as an event of the endpoint the context's resource is published at: tool `recall`, era `stateless` (a REST request has no session), stored with route `rest` and caller `bound_key` (public MCP calls: `mcp`, `anonymous`). It is recorded when the key's own per-minute limit refuses it (`rate_limited`), when it fails (`error`), is cancelled, or answers (`success` / `degraded`); a search the workspace's capacity lock or slot suspension refuses is not, as on the MCP endpoint. Anonymous REST searches are never recorded; nor are searches of a context whose endpoint is not serving (none published, the slug moved, revoked), or with a key whose plan gives bound keys no allowance. The route and caller are not part of schema v1: **the export stream does not carry REST rows**; a gap row can also count dropped REST events.

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

**Usage budget ([#2805](public-usage-budget.md)).** A call refused because the workspace's seven-day budget is used is `rate_limited` with `error_code: "usage_budget_exhausted"` (the per-minute refusal is `rate_limited` / `rate_limit_exceeded`: split `rate_limited` by `error_code`). A call refused because the budget could not be charged is `error` with `error_code: "usage_budget_unavailable"` (its response says `temporarily_unavailable`). Events therefore count more than the budget's `calls_used`: they include refusals, and the budget counts only admitted calls.

### Never recorded

No query text, no tool arguments, no result text, no memory ids, no document titles, summaries, URLs or document ids, no raw session id, no IP address, no user agent, no request headers, no workspace, context or resource names, and no credentials. The public endpoint has no credentials to record.

### Session details (off by default)

With `MCP_PUBLIC_TELEMETRY_SESSION_DETAILS_ENABLED=true`, session-era events also carry:

- `session_key` — an HMAC of the UTC date, the endpoint and the server-minted session id under `AUDIT_HMAC_KEY` (validated at startup in production). It is stable within one UTC day and changes the next day; the raw session id is never stored. **Session counts are connections, not people:** one person can open many sessions, and one session can serve many people behind one client.
- `client_family` — the name the client declared in `initialize` (`clientInfo.name`), lower-cased and reduced to `[a-z0-9._-]`, at most 32 characters. It is **unverified**: any client can declare any name. Every export page says so with `"client_family_verified": false`.

Stateless calls have neither. With the setting off (the default), both are always `null`: the declared client name is kept only in the session's memory and never stored.

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

A daily job (04:50 UTC, on the scheduler leader) deletes events older than `MCP_PUBLIC_TELEMETRY_RETENTION_DAYS` (default 90), gap rows included, and the document keys of resources that have had no live public endpoint since before that cutoff. Daily visitor rows ([#3035](https://github.com/kagura-ai/memory-cloud/issues/3035)) go in the same run once their UTC day is before the cutoff's day.

## Reading the stream: the commit-safe cursor

Events are read in pages in `(txid, id)` order: `txid` is the database transaction that wrote the event, `id` its storage order. A page only contains events whose transaction is older than every transaction still running on the database server when the page is read. Every such transaction has finished, so that set of events can no longer change, and a cursor that has passed it can never miss an event that commits later: a later commit always sorts after the cursor. A cursor that only followed insertion ids would skip an event whose transaction took an id first and committed last.

- A cursor is opaque. An omitted cursor starts at the oldest retained event; `next_cursor` is always returned: the last event of the page while `has_more` is true, otherwise a position at the publication horizon (so a reader that is caught up keeps up even while its scope has no events), and replaying a cursor returns the same events plus any sealed since.
- A long-running write transaction anywhere on the database server (a migration, a long maintenance run) delays publication until it ends: events are late, never lost. A page reports `coverage.unsealed_events`, the number of committed events not yet published, so "nothing happened" and "not published yet" can be told apart.
- **`cursor_expired` (410).** Retention advances a purge mark to the last deleted `(txid, id)`. A cursor below that mark may have missed purged events; it is refused with `error: "cursor_expired"` and `details.oldest_cursor`, where the reader restarts knowing there is a hole. A malformed cursor is `400` `cursor_invalid`.

A page envelope: `{schema_version, deployment_id, as_of, events, next_cursor, has_more, coverage: {gaps_in_page, dropped_in_page, unsealed_events, oldest_retained_at, retention_days, writer_enabled}, client_family_verified: false}`. `deployment_id` is the `DEPLOYMENT_ID` setting.

## Health

`GET /api/v1/resources/{resource_id}/public-mcp/usage-health` (workspace owner) returns the live endpoint's events of the last 24 hours by outcome, the deployment's gap rows in that window, and the writer state of the API process that answered (`writer.process_local: true`: with several API processes, each has its own queue). The gap rows are the persistent view of loss; the writer numbers are a snapshot of one process.

## Statistics for the owner

`GET /api/v1/resources/{resource_id}/public-mcp/usage-stats?days=7` ([#3035](https://github.com/kagura-ai/memory-cloud/issues/3035)) gives the workspace owner the numbers of the resource's **live** public endpoint, read from these events. It accepts what the other publication routes accept: a web session, the owner's workspace API key, an agent-bound key of the owner (within its binding), an OAuth token with the read scope. Admins, members, viewers, connector keys and public-bound keys are refused (403). An unknown slug, a resource without a live endpoint, and a context the credential cannot read are all the same `404`.

`days` is 1–90 (default 7, `422` otherwise); the window is at most the event retention (`window_days`), ends today and is counted in UTC calendar days.

```json
{
  "resource_id": "catalog", "endpoint_id": "…", "window_days": 7,
  "first_day": "2026-11-21", "last_day": "2026-11-27", "retention_days": 90, "recording_enabled": true,
  "rest_recording_enabled": true, "visitors": {"status": "ok", "counting_enabled": true},
  "totals": {"calls": 42, "searches": 30, "search_outcomes": {"success": 25, "degraded": 3, "denied": 0, "rate_limited": 2, "invalid_request": 0, "error": 0, "cancelled": 0},
             "calls_by_route": {"mcp": 30, "rest": 12}, "searches_by_route": {"mcp": 20, "rest": 10},
             "searches_answered_by_route": {"mcp": 19, "rest": 9}, "calls_by_caller": {"anonymous": 30, "bound_key": 12}},
  "days": [{"date": "2026-11-21", "calls": 5, "searches": 4, "search_outcomes": {"success": 4, …},
            "calls_by_route": {…}, "searches_by_route": {…}, "searches_answered_by_route": {…}, "calls_by_caller": {…}, "visitors": 3}, …],
  "documents": {"status": "ok", "publication_serving": true, "searches_scanned": 28, "scan_limit": 20000,
                "scanned_since": "2026-11-21T00:00:12Z",
                "items": [{"doc_key": "…", "searches": 12, "best_rank": 1, "title": "Trail shoe", "source_url": "https://example.com/p/1"}]},
  "generated_at": "2026-11-27T10:00:00Z"
}
```

- The numbers are **calls to the public MCP endpoint and, while `PUBLIC_SEARCH_TELEMETRY_ENABLED` is on, REST public searches of the context made with a key bound to it** (`calls_by_route`: `mcp`, `rest`; `rest_recording_enabled` says whether the REST ones are recorded now). Anonymous REST public searches, and searches of a context whose endpoint is not serving, are not included; every REST search is in the context's "Public API Usage" numbers.
- A **call** is one `tool_call` event of the endpoint: any tool, any outcome, refusals included. A **search** is a call of `recall`; `searches` and `searches_by_route` count every search whatever its outcome, `searches_answered_by_route` the answered ones (`success` or `degraded`); `search_outcomes` has every outcome as a key, per day and in `totals`. Every route and caller is a key too.
- `days` has exactly `window_days` entries, oldest first, zero-filled. A zero can also mean that recording was off (`recording_enabled` says whether it is on now) or that the events were already deleted under a shorter retention.
- **Calls and visitors.** `calls_by_caller` tells `anonymous` calls (the public MCP endpoint) from searches made with a `bound_key`. One visitor can make many calls. `days[].visitors` is the number of distinct visitor values servers reported with a bound key that UTC day ([Daily visitors](#daily-visitors)); `null` when they could not be read (`visitors.status: "unavailable"`), and `visitors.counting_enabled` says whether they are counted now. Your application decides what one visitor is: one value per page view counts page views. There is no total over the window: distinct values of different days cannot be added up.
- **Cached answers are not counted.** A cache in front of the server that answers a call itself leaves no event.
- **Undercount.** Calls the server could not record are missing (see the gap rows above); the [health route](#health) shows them.
- `documents.items`: at most 10 documents returned by `recall` calls that ended `success` or `degraded`, on both routes together; `searches` = how many of those calls returned the document, `best_rank` its best position; ordered by `searches`, then `best_rank`. They are computed from the newest `scan_limit` such calls of the window, starting at `scanned_since` (the time of the oldest of them); `searches_scanned` equal to `scan_limit` means older searches were not read. A document has the same key on both routes. For a REST search, a returned row that the endpoint does not serve (a member's note in the context) keeps its rank but has no key.
- `title` and `source_url` are what the public endpoint itself shows for the document (for an unedited feed record its title or question and URL), and only **while the endpoint serves right now**; otherwise `publication_serving` is `false` and every title and URL is `null`. While serving, a document that was deleted or is no longer public has `title: null`. No document id or memory id is ever returned.
- `documents.status` and `visitors.status` are `unavailable` when that part could not be read in time; the counts are still returned.
- Bounds: one read has a time budget of about six seconds, each statement a timeout of at most five seconds; a workspace can read its statistics 30 times per minute (`429` `RATE-001` with `Retry-After: 60`). When the day counts cannot be read the answer is `503` `usage_stats_unavailable` with `Retry-After: 60`. The route writes nothing.

Statistics exist for the live endpoint only: publishing again creates a new endpoint that starts at zero.

## Daily visitors

A server that calls the REST public search with a key bound to the context may send `Kagura-Visitor: <value>` with each search ([#3035](https://github.com/kagura-ai/memory-cloud/issues/3035); format and examples in [API Reference › Public Read API](api-reference.md#visitors-kagura-visitor)). The owner's statistics then count distinct visitors per UTC day.

- **Who reports them.** Only holders of a valid key bound to the context, at most one value per search and within the key's per-minute limit; an anonymous caller's header is never read, so it cannot change the count. The owner stops a key's reports by revoking the key, without unpublishing.
- **What is stored.** The value (1–64 characters of `[A-Za-z0-9_-]`) is turned at once into a 32-hex key: an HMAC of the UTC day, the endpoint and the value under `AUDIT_HMAC_KEY`; the value itself is never stored or logged. The same value on another day or another endpoint gives another key, and the API key that reported it is not part of it: two keys of one application that send the same value count once. Rotating `AUDIT_HMAC_KEY` during a day can count a value twice that day.
- **Stored apart from the call records.** Visitor keys go to their own table (`public_mcp_daily_visitors`: endpoint, day, key, workspace), never in the transaction that writes events, and in groups: a group of one endpoint and UTC day is written once at least ten distinct values are pending, after that UTC day ends, and when the server stops. So the current day's count can be behind; on an endpoint with fewer than ten visitors a day it appears after the UTC day ends. A process crash loses that process's pending values.
- **Only answered searches** (`success` or `degraded`) count a visitor, so the answered searches, the documents and the visitors share one base. A malformed or repeated header is ignored and logged by reason only (`repeated`, `format`).
- **No total.** Distinct values of separate days are not added up.
- **Retention.** Visitor rows are deleted by the daily retention job once their day is before the events' cutoff day (`MCP_PUBLIC_TELEMETRY_RETENTION_DAYS`).

## What a public call leaves on the server

This is what the application keeps. A reverse proxy or load balancer in front of it keeps its own access log, whose retention is the operator's.

For a call to the public MCP endpoint:

1. **One usage event** per completed `tools/call`: tool (one of the five names or `other`), outcome, error code, protocol era and version, duration, result count, continuation flag and opaque document keys — never query text, arguments or result text. Kept `MCP_PUBLIC_TELEMETRY_RETENTION_DAYS` (default 90). For the owner's statistics and health, and the export stream.
2. With `MCP_PUBLIC_TELEMETRY_SESSION_DETAILS_ENABLED` (off by default): a per-day session HMAC and the reduced declared client name on session-era events, kept as long as the events ([Session details](#session-details-off-by-default)). For counting sessions in the export.
3. **The client's declared name** (`clientInfo.name`, cut to 200 characters): in the session's memory for the life of the session; stored only as in item 2.
4. **The map from a document key to the resource's own document id**: kept while the resource exists and has, or recently had, a live public endpoint; deleted with the resource, and by the daily retention job once the resource has had no live endpoint since before the cutoff. For resolving a key to a title for the owner.
5. **Application log lines**: one per call with the endpoint's identity, the requested tool name as sent (cut to 64 characters), the outcome and the timing — no query text, memory id, session id, document id or client address. A refusal logs the tool name; ignored URL parameters are logged by name. Kept as long as the operator keeps application logs. For operations.
6. **For a search**: the query is sent to the embedding provider the workspace uses; its embedding vector is cached for 24 hours under a hash of the query text (no caller identity: the same text from anyone hits the same entry), for speed and cost. One cost row (model, tokens, cost; no text) is written for the owner's cost report; the application sets no time limit on these rows.
7. With the log level at `debug` (not the default): the first 50 characters of a search query in the application log.
8. **Per-minute counters** keyed by the endpoint and, in a session, by the server-minted session id (60 seconds); the session itself for its lifetime. No IP address. For the rate limits.
9. With `MCP_PUBLIC_USAGE_BUDGET_ENABLED` on: the workspace's count of admitted calls for the current seven-day period; no caller data. For the [usage budget](public-usage-budget.md).

For a request to the REST public search:

10. One `usage_stats` row per request: the path (it contains the context id), method, status, response time and, for a key bound to the context, the key's owner and key id; the application sets no time limit on these rows. For the "Public API Usage" numbers and per-key attribution. With `PUBLIC_SEARCH_TELEMETRY_ENABLED` on, a search made with a key bound to a context whose endpoint is serving also leaves **one usage event** of that endpoint (route `rest`, caller `bound_key`; no key id or user id on it), and, when the server sent a well-formed `Kagura-Visitor` value and the search was answered, one daily visitor key per day and value ([Daily visitors](#daily-visitors)). An anonymous search, or one of a context without a serving endpoint, leaves no usage event.
11. Log lines with the query's length and a keyed hash of the query, not the text, and the stage timings of the search; the per-minute counters; the usage budget charge for an anonymous search of a published context. No caller is reranked. Items 6 and 7 apply to its search as well. The shipped single-server proxy drops `Kagura-Visitor` from its own logs (any other proxy or CDN in front of the server must drop it too).

For either:

12. A request that carries a valid web sign-in cookie of the same deployment is also recorded as that signed-in user's request (path, method, status, response time), like any request of a signed-in user; the public endpoint itself does not read the cookie.
13. **The application server's access log** (uvicorn's, on by default): one line per HTTP request with a client address and the request line — method, path with the endpoint's address, and query string. Kept as long as the operator keeps application logs; uvicorn's `--no-access-log` turns it off. For operations. The address is the TCP peer's, unless that peer is a trusted proxy:
    - with nothing in front of the server, the visitor's address;
    - behind a reverse proxy that uvicorn does not trust, the proxy's address;
    - behind a trusted proxy, the visitor's address as the proxy forwards it (`X-Forwarded-For`). uvicorn trusts the addresses in `FORWARDED_ALLOW_IPS` (or `--forwarded-allow-ips`), by default `127.0.0.1` and `::1`, so a proxy that forwards over the loopback interface is trusted without any setting.
