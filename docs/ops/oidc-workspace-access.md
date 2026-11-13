# OIDC workspace access inspection

Pre-registered OIDC clients can inspect the signed-in account's current workspace
membership and effective resource permissions. Enable and register the client as
described in [OIDC provider operations](oidc-provider.md), including the
`workspaces:read` scope. These endpoints remain unavailable while `OIDC_ENABLED=false`.

Send the opaque OIDC **access token** in `Authorization: Bearer ...`. ID tokens,
refresh tokens, browser cookies, API keys and memory/MCP OAuth credentials are
not accepted. This scope grants inspection only; it does not authorize memory
reads, writes, billing changes or service integrations.

## List memberships

`GET /api/v1/me/workspaces?limit=100&cursor=<workspace UUID>`

The default limit is 100 (1–100 inclusive). Results contain only the subject's
memberships in live workspaces, ordered by workspace UUID. Pass `next_cursor` as
the next request's `cursor`; `null` means the end. A page is a current view rather
than a transaction spanning requests, so membership changes can affect later pages.

```json
{
  "subject": "stable-kmc-user-id",
  "workspaces": [
    {"workspace_id": "11111111-1111-4111-8111-111111111111", "name": "Example", "workspace_role": "member"}
  ],
  "next_cursor": null
}
```

An account without memberships receives an empty list. The account's selected
workspace in the KMC UI is never used to scope this API.

## Inspect effective access

`GET /api/v1/me/workspaces/{workspace_id}/access?context_id=<context UUID>`

```json
{
  "subject": "stable-kmc-user-id",
  "workspace_id": "11111111-1111-4111-8111-111111111111",
  "workspace_role": "member",
  "workspace_permissions": {"billing.manage": false, "usage.read": true},
  "resource": {
    "context_id": "22222222-2222-4222-8222-222222222222",
    "permissions": {"read": true, "sync.manage": true}
  }
}
```

Omit `context_id` to inspect workspace membership alone (`resource` is then
`null`). `billing.manage` requires both the canonical workspace owner and an
`owner` membership; admin or system-admin status alone never confers it.
`usage.read` is available to existing members even when their plan has no seat.

Resource inspection reuses the existing KMC permission service: privacy and
linked ownership, membership, context reach lists, explicit context roles and
plan seats all apply. The context must belong to the workspace in the path.
An effective viewer can read but cannot manage synchronization. Effective
editor/owner access can manage synchronization only while the context has a
writable plan slot and content capacity is unlocked. Capacity-locked resources
can still be inspected, with both flags false. A context without a writable
slot remains readable. Integration entitlements are a separate check.

The provider rechecks the access token, original grant, client, account and
scope on every call. Revocation, expiry, membership changes, ownership transfer
and permission/plan changes take effect on the next request. Treat a response
as a point-in-time inspection, never as a durable authorization grant; a
mutation API must reauthorize the operation itself.

## Errors and operational behavior

Every response has `Cache-Control: no-store` and `Pragma: no-cache`. Errors use
`{"error":"<code>"}` without reflecting credentials, validation inputs or DB
diagnostics. Unknown and duplicate query parameters are rejected.

| HTTP | Error | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | Invalid UUID, limit or query parameter. |
| 401 | `invalid_token` | Missing, wrong-kind, expired, revoked or unknown credential (`WWW-Authenticate: Bearer`). |
| 403 | `insufficient_scope` | A live token, original grant or current client lacks `workspaces:read`; Bearer challenge names the required scope. |
| 404 | `not_found` | Disabled provider, missing/deleted workspace, nonmember, or inaccessible/foreign/deleted context. |
| 429 | `temporarily_unavailable` | Request budget exceeded; honor `Retry-After`. |
| 503 | `temporarily_unavailable` | Authentication/authorization dependency unavailable; retry later. |

These reads use the async request DB session, independently of the synchronous
Authlib protocol pool. `OIDC_RATE_LIMIT_PER_MINUTE` also bounds these operations
per trusted ASGI peer, with separate `workspace-list` and `workspace-access`
buckets. Workspace and context IDs do not create new buckets. Redis failure
refuses authentication work before a DB connection is opened. Proxy identity
uses the deployment's existing trusted-proxy policy, never raw forwarded headers.

No new tables, migrations or runtime dependencies are required.
