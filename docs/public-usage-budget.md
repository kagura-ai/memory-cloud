# Public usage budget — Issue #2805

A deployment can bound the anonymous calls that all public MCP endpoints of a
workspace serve together: **one budget per workspace, per seven-day period**.
This page is the contract of that budget: what counts, when a period starts
and ends, what an anonymous caller sees when it runs out, what the owner can
read, what happens when the budget store fails, and how an operator inspects
and refills it.

The per-minute buckets of [Public MCP endpoints](deployment.md#public-mcp-endpoints--issue-2675)
stay as they are. The budget is a second, separate limit.

## What it bounds, and what it does not

- It bounds the **operator's cost** of anonymous serving: at most `limit`
  admitted public `tools/call` (plus the counted REST searches below) per
  workspace per period, whatever the number of endpoints, sessions or clients.
- It does **not** protect a workspace's availability. The budget is
  anonymous and workspace-wide: anyone can spend it. At the default limit of
  25,000 and the default 120 calls per minute per endpoint, one endpoint is
  drained in about 3.5 hours (sooner with several endpoints), and the
  workspace's public endpoints then refuse `tools/call` until the period ends
  or an operator refills it. Per-client limiting in front of the deployment
  (a reverse proxy's own rate limit) is the operator's tool against that.
- There is no daily cap, no purchased credit, no add-on bonus and no plan
  tier in it: one flat number per deployment. The plan fields
  `public_calls_per_week` / `public_calls_per_day` keep their current meaning
  and are not this budget.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `MCP_PUBLIC_USAGE_BUDGET_ENABLED` | `false` | Switch. Off: no statement runs, nothing is counted, nothing is refused, and owner reads report `state: "disabled"`. Stored rows stay. |
| `MCP_PUBLIC_USAGE_BUDGET_CALLS` | `25000` | Counted calls per workspace per seven-day period, all its public endpoints and the counted REST searches together. `1`–`1,000,000,000`; `0` or a negative value is refused at start-up (it never means "unlimited"). |

The limit is not stored: a changed value applies from the next call. Lowering
it below what a running period already used exhausts that period.

The period length (seven days), the lock timeout of the charge (2 s), the
overall bound of the charge (5 s) and the cap of the anonymous retry hint
(3,600 s) are code constants, not settings.

## Period and rollover

- A workspace has no period until its first counted call. That call starts a
  period at its own time (`period_started_at`), and the period ends seven days
  later (`resets_at = period_started_at + 7 days`).
- After a period ended, the **next counted call** starts the next period at
  its own time, with a fresh count of 1 and `period_seq + 1`. There is no
  fixed grid and no background job: a workspace that is quiet for 15 days
  starts its next period at the call that ends the silence.
- All times are the database clock (`statement_timestamp()` in UTC), the same
  for every API process; no application clock decides a period.
- If the database clock steps back before `period_started_at`, the running
  period simply continues.
- `period_seq` names the period: it is 1 for the first and grows by exactly 1
  at each rollover, also when many calls arrive at the rollover at once.

## Counting rules (frozen)

A call counts **if and only if it was admitted to run a tool**. The count is
taken before the tool runs and is never refunded.

| Request | Counted |
|---|---|
| `tools/call` handed to a tool: success, degraded, empty result | yes |
| the tool refuses a value (`invalid_argument`, `memory_not_found`) | yes |
| tool error, masked workspace refusal, timeout | yes |
| cancelled after the count (client gone, server shutdown) | yes |
| a retry of anything (same JSON-RPC id or not) | each request on its own |
| unknown or hidden tool, `arguments` not an object, unknown argument names | no (refused before any store) |
| refused by the per-minute call or session bucket, or Redis unreadable | no (the budget is never reached) |
| wrong `context_id` | no |
| refused by the budget | no (the count does not move); it has spent a per-minute slot |
| budget store failure | no, or once when the commit landed and its acknowledgement was lost |
| parse error, batch, oversize, bad envelope, session-era `-32602`, notification, `GET`, `DELETE` | no |
| `initialize`, `tools/list`, `ping`, `server/discover` | no; still served at exhaustion |
| `404` not published, `503` plan pause or capacity lock | no |
| anonymous REST search of a context with a live public MCP publication, admitted by its per-minute bucket | yes |

Order on the public MCP path: (1) the name must be in the URL's tool view,
(2) `arguments` must be an object, (3) argument names must be known,
(4) per-minute buckets (fail closed), (5) `context_id` must be the bound
context, (6) **budget charge (fail closed)**, (7) the tool.

## Entry points that share one budget

The budget key is the workspace id of the endpoint the server loaded for the
request, never anything the client sends (not the address, a header, the
session id, `?tools=`, `clientInfo` or a network address).

Every `/mcp/public/{public_id}` of the workspace counts against the same
budget: both protocol eras, every `?tools=` view, the endpoint of a sync
installation (an ordinary public endpoint), and the anonymous REST search
below. The stored period is untouched by republish (a new endpoint id),
unpublish, a slug change, sync unlink, owner transfer, a plan change, add-on
or entitlement cancellation, soft delete and restore; only a hard delete of
the workspace removes it.

**Rule for verified domain aliases (#2809):** an alias must resolve to the
same serving endpoint and enter the public MCP transport and its `tools/call`
dispatch unchanged. The budget has no address or domain dimension, the
per-minute bucket stays keyed by the endpoint id, and an alias must not add a
per-domain counter or allowance.

## What does not count, and why that opens no anonymous bypass

A read is exempt only when a credential that the workspace or the operator
issued and can revoke was verified, **and** the route refuses the same
request without it. A header, cookie, query flag, `Origin` or `clientInfo`
never turns a public-path call into a management call: the public MCP path
never reads `Authorization` or `Cookie`, so an owner calling their own public
address is counted like anyone. There is no owner preview on the public path.

Exempt (the authenticated control-UI read projection):

- the owner routes `/api/v1/resources/{resource_id}/public-mcp` and
  `/public-mcp/usage-health` (workspace owner);
- sync installation inspection (signed-in browser client, `workspaces:read`);
- the observability API (observation credential);
- the authenticated `/mcp` tools (member token; the workspace's daily MCP
  quota applies there);
- share-key recall;
- REST public search with a public-bound API key, or with a signed-in session
  of the context's own workspace.

Not counted and still served when the budget is exhausted: `initialize`,
`tools/list`, `ping`, `server/discover`, `GET /api/v1/public/{context_id}/info`,
and anonymous REST search of a public context that has **no** live public MCP
publication (the older REST public search; its own per-minute bucket applies).

## Refusals

### Public MCP `tools/call`

Both eras answer HTTP `200` with a JSON-RPC result marked `isError: true`,
like the per-minute refusal. The text content is:

```json
{"status": "error", "error": "usage_budget_exhausted",
 "message": "This public endpoint has used its call allowance for the current period. Try again later.",
 "retry_after_seconds": 3600}
```

`retry_after_seconds` is the time to the end of the period, rounded up to a
minute, at least 60 and **at most 3,600**. The refusal carries no count, no
limit, no remaining calls, no plan, no workspace and no absolute time, and no
header on the MCP path. A client that retries after the hint either gets an
answer (the period ended, or an operator refilled it) or another refusal.

When the budget store cannot be reached or does not answer in time, the call
is refused with the existing body
`{"status": "error", "error": "temporarily_unavailable", ..., "retry_after_seconds": 60}`.

### Anonymous REST search

`POST /api/v1/public/{context_id}/search` without a signed-in session and
without a bound key, on a context with a live public MCP publication, while
the budget is on:

| Status | `error` | Header | When |
|---|---|---|---|
| `429` | `usage_budget_exhausted` | `Retry-After: <n>` (60–3,600) | the period's calls are used; `details.retry_after` = n |
| `503` | `temporarily_unavailable` | `Retry-After: 60` | the budget store failed, or the per-minute bucket could not be read |
| `429` | `RATE-001` | `Retry-After` | the per-minute bucket refused it (as before; not counted) |

On this path an unreadable per-minute bucket refuses the request instead of
counting it or serving it uncounted: a unit is charged only to a request a
per-minute bucket admitted. Outside the counted path the REST bucket keeps
failing open as before.

### Stable codes

| Code | Where |
|---|---|
| `usage_budget_exhausted` | anonymous MCP refusal, anonymous REST `429` |
| `temporarily_unavailable` | budget store failure (MCP body and REST `503`) |
| `usage_budget_unavailable` | telemetry `error_code` and the log only, never in a response |

In the [usage events](public-mcp-usage-events.md), a budget refusal is outcome
`rate_limited` with `error_code` `usage_budget_exhausted`; a store failure is
outcome `error` with `error_code` `usage_budget_unavailable`.

## Owner read

`GET`, `POST` and `PATCH /api/v1/resources/{resource_id}/public-mcp` carry a
field `usage_budget`. It is **workspace-wide**: every public endpoint of the
workspace shows the same numbers. Example (`https://example.com/api/v1/resources/catalog/public-mcp`):

```json
{"usage_budget": {
  "state": "active",
  "limit": 25000, "period_days": 7,
  "used": 1200, "remaining": 23800,
  "period_started_at": "2026-11-10T08:00:00Z",
  "resets_at": "2026-11-17T08:00:00Z",
  "period_seq": 3,
  "as_of": "2026-11-14T00:00:00Z"}}
```

| `state` | Meaning | Fields |
|---|---|---|
| `disabled` | the switch is off | `limit`, `used`, `remaining`, the times and `period_seq` are `null` (never `0`) |
| `idle` | no period runs (none yet, or the last one ended) | `used` 0, `remaining` = `limit`, times and `period_seq` `null`; the next counted call starts a period |
| `active` | a period runs and has calls left | `used`, `remaining = limit - used`, `resets_at = period_started_at + 7 days` |
| `exhausted` | a period runs and `used >= limit` | `remaining` 0 |

`as_of` is the database clock of the read. The read never writes: it never
starts a period. `serving` keeps its meaning (the endpoint is published and
the operator switch is on) and does not include exhaustion.

The observability API, the sync installation view, `/api/v1/usage`, the MCP
`get_usage` tool and `/api/v1/system/info` do not carry the budget.

## Outage policy

- Any failure of the charge refuses the call (`temporarily_unavailable`,
  60 s): a connection error, a lock wait over 2 s, the whole charge over 5 s,
  an unexpected error. A store failure is never reported as exhaustion and
  never as success.
- A commit whose acknowledgement was lost can count a call that was then
  refused; never the reverse.
- Postgres already serves every public request (the publication is loaded
  from it), so the budget adds no new dependency: when Postgres is down
  nothing is served.
- The budget uses no Redis.

## Operator notes

- **Deploy order:** run the database migration (`f48_2805_public_usage_budgets`)
  before setting `MCP_PUBLIC_USAGE_BUDGET_ENABLED=true`. New code with the
  switch on and no table refuses every public `tools/call` (fail closed). In
  a rolling deploy, processes that still run the old code do not count.
- **Drain:** see "What it bounds". The log lines below show a drain coming.
- **Inspect and refill** a workspace's budget, where the API runs (same
  environment):

  ```bash
  python -m src.cli.public_usage_budget show <workspace-id>
  python -m src.cli.public_usage_budget reset <workspace-id>                # plan (read-only)
  python -m src.cli.public_usage_budget reset <workspace-id> --apply --yes  # refill
  ```

  `show` prints the owner read as JSON. `reset --apply` sets the count of the
  running period to 0 and keeps the period (its start, `period_seq` and reset
  time); it writes the `public_usage_budget_reset` log line and nothing else.
  A workspace without a stored period exits 1 and nothing is created. SQL
  equivalent:

  ```sql
  UPDATE public_usage_budgets
     SET calls_used = 0, updated_at = timezone('UTC', statement_timestamp())
   WHERE workspace_id = '<workspace-id>';
  ```
- **Turning it off** stops all counting; rows stay. Turning it back on
  continues the stored period, or the next call starts a new one when it
  ended. Calls made while it was off are not counted.
- **Dashboards:** usage-event outcome `rate_limited` now covers per-minute and
  budget refusals; split by `error_code`. Usage events count more than the
  budget (they include refusals).

## Data lifecycle

One row per workspace in `public_usage_budgets`: `workspace_id`,
`period_started_at`, `calls_used`, `period_seq`, `updated_at`. No person, no
address, no endpoint, no client and no content. A workspace hard delete
removes the row (foreign key cascade); a soft delete keeps it; account erasure
has nothing to erase in it; it is in no export. Downgrading the migration
drops the table: counts and periods are lost and every workspace starts a
fresh period at its next counted call.

## Log events

Aggregates only: never an address, user agent, session id, client name, query
text, `public_id` or exception message. No line per refused call.

| Event | Level | Fields | When |
|---|---|---|---|
| `public_usage_budget_period_started` | info | `workspace_id`, `period_started_at`, `period_seq`, `limit` | an admitted call started a period |
| `public_usage_budget_threshold` | warning | `workspace_id`, `level`, `calls_used`, `limit`, `period_seq` | an admitted call reached 80, 95 or 100 % of the limit (once each per period; when several levels fall on the same call, the highest only) |
| `public_usage_budget_unavailable` | warning | `workspace_id`, `error_type` | the charge failed |
| `public_usage_budget_reset` | warning | `workspace_id`, `period_seq`, `calls_used_before` | an operator refill was applied |
