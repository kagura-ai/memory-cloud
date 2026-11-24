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

### Kagura Public eligibility

Add `eligible_for=kagura-public` to list only the workspaces the account can
connect to Kagura Public: the account is the canonical workspace owner, holds an
`owner` membership, and the workspace is on the Max plan now. The filter runs in
the query, so `limit` and `next_cursor` page through eligible workspaces only.
The access route below takes the same parameter and answers `404 not_found` for
a workspace that does not meet the rule, exactly as for one the account cannot
reach. Any other value is `400 invalid_request`. Without the parameter both
routes answer as before.

"On the Max plan now" means the workspace's current plan name; the billing
access window and integration licenses are not consulted by this filter.

The plan page's notice with the link back to the application uses the same
rule: it shows only to the canonical owner holding the `owner` role, and says
the workspace is on the Max plan only when its current plan name is `promax`
([Link back to the application on the plan page](../deployment.md#link-back-to-the-application-on-the-plan-page-issue-2950)).
The application still checks eligibility with this parameter by itself.

A slot-suspended workspace that meets the rule stays listed. While slot
suspension is enforced, its access answer reports `read`, `sync.manage` and
`usage.read` as `false` and keeps `billing.manage`, so a client checks the
access route before offering a workspace as usable.

A server without this parameter rejects it as an unknown query parameter
(`400 invalid_request`). Deploy the server before any client sends it; a client
must not retry without the parameter, which would return every membership.

## Inspect effective access

`GET /api/v1/me/workspaces/{workspace_id}/access?context_id=<context UUID>&eligible_for=kagura-public`

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

Both parameters are optional. Omit `context_id` to inspect workspace membership alone (`resource` is then
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
buckets. Workspace and context IDs do not create new buckets. Over the peer
budget, a request whose access token this server issued is counted for the
token's client and user instead (one indexed lookup on a separate session, before the
request's own session opens); an unknown token keeps the 429. Redis failure
refuses authentication work before a DB connection is opened. Proxy identity
uses the deployment's existing trusted-proxy policy, never raw forwarded headers
(see [OIDC provider](oidc-provider.md#protocol-burst-limits)).

The budget does not depend on a workspace or a plan. The workspace ID in the
path does not choose the bucket, and every plan gets the same budget per
operation (`workspace-list`, `workspace-access`). Each request is counted per
peer; over the peer budget, a request with an access token this server issued
is admitted against a bucket of its client and user instead. That bucket gives
more allowance to a client whose users share a peer; it is not a second limit
on top of the first. A client application that reads many workspaces paces its
reads instead of re-reading all of them every minute. For example, it reads a
workspace's `/access` once, caches it, and reads it again when it acts on that
workspace. On a 429 it waits for `Retry-After` before it retries. An operator
gives more by raising `OIDC_RATE_LIMIT_PER_MINUTE`. That setting also bounds
the protocol endpoints (`/oidc/authorize`, `/oidc/token`, `/oidc/revoke`,
`/oidc/userinfo`) and the sync routes.

No new tables, migrations or runtime dependencies are required.
