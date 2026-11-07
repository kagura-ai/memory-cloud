# Resource-scoped management API

This unreleased contract lets an owner delegate a small set of operations to an external service without giving it an owner API key. Observation credentials, public MCP, resource tokens and management grants are separate credential types.

## Provisioning

An owner calls `POST /api/v1/resource-management-grants` using the existing owner authentication. The body names `resource_pk`, `context_id`, `endpoint_id`, an operation list, explicitly approved field names, `ttl_days` (1–90), `quota_events_per_hour` (1–10000), and `max_active_tokens` (1–100). Deployment identity and current workspace come from the server. The endpoint and context must currently be public, live and bound to that resource. Connector resources are excluded.

Operations: `schema:read`, `events:upsert`, `events:delete`, `tokens:read`, `tokens:create`, `tokens:update`, `tokens:revoke`. Grant scope cannot be edited. Only a SHA-256 verifier of the generated `kagura_mgmt_…` secret is stored. The secret appears once in the creation response with `Cache-Control: no-store`.

`tokens:create` additionally requires `acknowledge_full_resource_token_authority: true`. A resource token has **full ingestion authority for its bound resource, including delete and fields beyond a submission grant's allowlist**. It has no expiry or per-operation scopes. It remains independent of the issuing grant's lifetime/revocation. Never enable token issuance merely because a service may submit a subset of fields.

`GET /api/v1/resource-management-grants` lists up to 100 recent grants without secrets. `DELETE /api/v1/resource-management-grants/{grant_id}` revokes one. To rotate, create and verify a new grant, switch the service's encrypted server-side configuration, then revoke the old grant. Approval is explicit again; rotation never silently widens an existing grant.

## Management requests

All paths below are under `/api/v1/resource-management`. Authenticate with `Authorization: Bearer <management-secret>`. Writes require `X-Management-Actor`, a 64-character lowercase hex pseudonym asserted by the authenticated external service. This is service-attributed auditing, not independent verification of the service's end-user identity. The service must enforce its own session, role and CSRF policies and generate this attribution on the server.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/capabilities` | Contract `resource-management/v1`, immutable identities, operations, expiry, limits and token semantics |
| GET | `/schema` | Approved scalar field names/types/required flags and schema digest |
| GET | `/tokens?limit=50&offset=0` | Bound resource's token metadata only |
| POST | `/tokens` | Metadata and one-time secret; `operation_id`, description and quota required/accepted |
| PATCH | `/tokens/{public_token_id}` | Update description/quota, with `operation_id` |
| DELETE | `/tokens/{public_token_id}` | Revoke, with JSON `operation_id` |
| POST | `/events` | Submit `operation_id`, `schema_hash`, and up to 100 events |

The origin must be an operator-approved HTTPS endpoint. Clients must not follow redirects or forward management credentials to arbitrary URLs. Maximum request/response contract size is 1 MiB; each event payload is at most 100000 UTF-8 bytes. Events use `op`, `doc_id`, integer `version`, UUID `idempotency_key`, and `payload`. An upsert requires an integer version from 1 to 2147483647 and nonempty payload; a delete accepts an optional version in the same range, with omission deleting all versions, and no payload. The supported submission field types are text, finite number, boolean and ISO date. Other field types must be handled by a separately approved integration. FieldDefinition is metadata, not JSON Schema or an authorization boundary: approval is explicit, and unknown/unapproved fields are rejected on the server.

Every operation rechecks grant expiry/revocation, its current issuing owner's membership, workspace plan, resource/context/publication identity and approved schema revision. Rebinding, unpublishing or schema drift requires new approval. Ingest also checks capacity and plan suspension. It uses the shared ingestion service and shared resource quota; the delegated path refuses Redis outages. Token minting uses the existing workspace mint lock/cap and adds the grant's resource token cap/quota ceiling. Existing owner routes retain `WorkspaceOwner` authentication.

## Receipts and errors

The external service creates an operation UUID once per confirmed action and reuses it for retries. Operations are scoped to the grant and recorded with a request digest, asserted actor and sanitized outcome, without source payloads or secrets. Changing the body or actor under an existing UUID returns 409. Event idempotency keys are additionally namespaced to the grant and resource before reaching the existing ingestion service.

A retry of token creation returns the existing metadata with `replayed: true` **without the secret**. After an uncertain result, inspect metadata and revoke an unusable token; never silently create another token. Replacement creation and old-token revocation are separate explicit operations.

Ingestion receipts distinguish `accepted`, `partial`, and `failed`, with accepted event IDs and index/error-code pairs. Acceptance means events are persisted; it does not prove the scheduler ran or that data is searchable (`searchable: unknown`). A response can fail after commit. Retrying with the same operation UUID returns the stored receipt without duplicate writes; processing progress must be observed separately. Schema drift/conflicting operation identifiers return 409, rate limits 429, lost authority 401/403, and unavailable strict quota reservation 503. Error bodies never echo submitted payloads.

Transactions serialize grant use/revocation and share the workspace token mint lock before locking resource identities. Read-only observation and public MCP paths are unchanged. Tests cover real-DB identity/role/plan/schema changes, field validation, one-time token display, replay, cross-resource token access, concurrent mint caps, oversized bodies, quota outages, scheduler failure and migration round trips.
