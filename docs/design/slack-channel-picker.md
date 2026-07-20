# Slack Channel Picker for Connector Settings

> **Status: design note (#1391) — not implemented.** Raw channel-ID entry is
> the deepest remaining UX debt in the connector settings dialog (#1388): the
> admin must leave the app, find a Slack channel ID, and paste it. This note
> designs a channel picker backed by the connector's stored bot token.

## Backend endpoint

```
GET /api/v1/workspace-connectors/{connector_id}/channels?cursor=<c>&q=<query>
```

- **Auth**: workspace admin (same `WorkspaceAdmin` dependency as the other
  `/workspace-connectors` routes); the connector must belong to the current
  workspace (workspace-predicated lookup, 404 on cross-workspace IDs — same
  contract as the settings PATCH).
- **Mechanism**: server-side proxy of Slack `conversations.list` using the
  connector's Fernet-decrypted bot token (`get_oauth_tokens()["bot_token"]`).
  The token **never reaches the browser** — this endpoint exists precisely so
  it doesn't have to.
- **Response**: `{ channels: [{id, name, is_private}], next_cursor }` — id and
  name only (plus the private flag for display); no member counts, topics, or
  other metadata (data minimization).
- **Pagination**: pass through Slack's cursor (`limit=200` per page);
  `q` filters server-side on the fetched page (Slack's API has no name filter;
  the UI filters client-side within loaded pages, `q` is optional sugar).

## Scope requirement and legacy installs

`conversations.list` for public channels requires the **`channels:read`**
scope, which IS in the default install scopes
(`settings.slack_oauth_scopes` = `channels:history,channels:read,groups:history,chat:write,team:read,users:read`).
Private channels additionally need `groups:read`, which is **not** currently
requested — so the v1 picker lists public channels only, and private channels
remain manual-ID entry (document this in the dialog copy).

Behavior when the token lacks the scope (legacy installs, manual binds of
older apps): Slack returns `missing_scope`. The endpoint maps this to a
structured `409 {error_code: "CONNECTOR-SCOPE"}`; the frontend degrades to the
manual-ID input with a notice ("再インストールで一覧選択が使えます"). Never a
raw 5xx.

## Rate limits and caching

`conversations.list` is Slack rate-limit **Tier 2** (~20 req/min per token).
A workspace admin repeatedly opening the dialog must not burn the budget the
worker may also need:

- Short server-side cache per connector: Redis key
  `slack_channels:{connector_id}:{cursor}` with ~60s TTL.
- On Slack `rate_limited`, surface `429` with `Retry-After` passthrough; the
  frontend shows the manual-entry fallback rather than spinning.

## Frontend

- The settings dialog's ingest-scope section gains a searchable multi-select
  (combobox) seeded with the current `channel_ids`; selection writes the same
  `channel_ids` PATCH as today (order-insensitive compare unchanged).
- **Manual-ID entry stays as a fallback lane** (private channels, scope-less
  legacy installs, Enterprise edge cases) — a toggle between "一覧から選択"
  and "ID を直接入力".
- Channel IDs already selected but not present in the fetched list (e.g. a
  private channel) render as opaque ID chips — never silently dropped by a
  save.

## Security summary

- Bot token: server-side only, decrypt-per-request, never logged.
- Response carries channel id/name only; no message content.
- Workspace-admin RBAC + workspace-predicated connector lookup (no
  cross-tenant channel enumeration).
- The endpoint is read-only and cache-bounded; it cannot mutate connector
  state.

## Non-goals

- No `groups:read` scope expansion in v1 (private-channel listing is a
  follow-up decision — scope creep on installed apps forces re-consent).
- No provider-generic picker abstraction yet — Discord/Teams pickers have
  different hierarchy shapes and arrive with their providers (see
  [multi-platform readiness](multi-platform-connectors.md)).

Refs: #1376, #1388, #1391.
