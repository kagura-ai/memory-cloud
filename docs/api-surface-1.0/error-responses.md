# Error Response Shapes — pre-1.0 surface enumeration

> Issue: #622 — pre-1.0 public API surface enumeration and freeze
> Enumerated at commit 20ae959a2c79cacd2cf7922512ad780f540e9c60 (main HEAD, 2026-06-12)

Method: exhaustive `grep -rn "error_code" backend/src --include='*.py'` (all 80 occurrences triaged), full read of `backend/src/utils/exceptions.py`, the exception handlers in `backend/src/api/main.py`, the MCP transport/tool error paths, and a per-file census of raw `raise HTTPException(` sites.

---

## Canonical shape

### 1. REST — structured errors (`MemoryCloudException` family)

Emitted by `memory_cloud_exception_handler` (`backend/src/api/main.py:313-339`) for every `MemoryCloudException` subclass:

```json
{
  "error": "<error_code>",
  "message": "<human-readable message>",
  "details": { "...": "..." }
}
```

| Field | Presence | Description |
|---|---|---|
| `error` | always | Machine-readable error code. Set per exception class; falls back to the exception **class name** if the constructor receives `error_code=None` (`exceptions.py:39`) — no current raise site exercises the fallback, but it is live behavior. ⚠ |
| `message` | always | Human-readable message (`exc.message`). |
| `details` | always (may be `{}`) | The exception's `**details` kwargs. **Exception:** for `AuthorizationError` and `AdminProtectionError` the handler forcibly serializes `details: {}` (CWE-639 defense in depth, `main.py:331`) — the private `reason` attribute is log-only and never serialized. |

HTTP status comes from `exc.status_code` (per-class; see catalogue). Notable headers:

- `DatabaseConnectionError` paths (incl. the `SQLAlchemyError` and connection-error handlers, `main.py:342-403`) add `Retry-After: 5` and return the same 3-field body via `_database_unavailable_response` (wraps `DatabaseConnectionError` → `DB-002`, 503). The connection-error handler is registered on `ConnectionRefusedError` / `ConnectionResetError` / `ConnectionAbortedError` / `BrokenPipeError` (not base `ConnectionError`).
- Rate-limit middleware (`backend/src/api/middleware/rate_limit.py:121-163`) returns the **same 3-field shape inline** (not via the handler): `RATE-001` 429 with `X-RateLimit-Limit/Remaining/Reset` + `Retry-After: 60`; `QUOTA-001` 429 with `Retry-After: 86400`. Since #1644 that inline body merges the exception's own `details` (so the gate annotation and `quota_type` survive) instead of replacing them with `{retry_after}`.
- Deprecated `/api/v1/attachments/*` returns the same 3-field shape inline as `RES-004` 410 with RFC 8594 `Sunset` / `Deprecation` / `Link` headers (`backend/src/api/routes/attachments.py:29-54`) — emitted inline precisely because the global handler does not propagate those headers.

> ⚠ Note vs. intent: issues #401/#602/#603/#604 standardized on this `{error, message, details}` shape (confirmed in the #602 PR body: callers read `exc.reason` instead of `exc.detail`; frontend consumes the uniform shape). The shape is **not** `{detail, error_code}` — `detail` is the *non-conforming* FastAPI default, below.

### 2. REST — raw `HTTPException` → canonical via the global handler (since #992 Phase 2)

Since #992 Phase 2 a global `StarletteHTTPException` handler (`api/main.py`) reshapes every raw `raise HTTPException` into the canonical envelope, so the **shape is frozen across the whole surface** even where a semantic code has not yet been assigned:

```json
{ "error": "HTTP-<status>", "message": "<the detail string>", "details": {} }
```

- `error` carries the reserved `HTTP-<status>` placeholder namespace (e.g. `HTTP-404`), distinct from the semantic `AUTH-`/`ADMIN-`/`VAL-`/`REQ-` codes. SDKs route on the shape uniformly today; per-site semantic codes are a post-1.0 improvement.
- `exc.headers` is passed through, so RFC 6750 `WWW-Authenticate: Bearer` challenges survive (`oauth.py`, `auth/dependencies.py`).
- Shape-only: the handler does NOT apply the CWE-639 deny-class `details` strip (a raw `HTTPException` carries no structured `details` — only a string `detail` → `message`). The remaining raw 403 detail strings were audited for forensic leakage in the #992 review; the one cross-workspace existence-oracle smell is tracked in **#1011**.

> **#992 (resolved on the REST surface):** the original census counted **366** raw raise sites. **Phase 1** converted 36 auth-boundary 401/403 route-level sites onto the canonical `MemoryCloudException` family (single-line **and** multi-line forms; `oauth.py:327/341` left raw for their RFC 6750 `WWW-Authenticate` header) and added the 422 `RequestValidationError` handler. **Phase 2** froze the *shape* of the remaining **~296** raw sites in one place via the global `StarletteHTTPException` stopgap handler above — the two-shapes-coexisting problem is gone. Assigning each placeholder `HTTP-<status>` a semantic `NAMESPACE-NNN` code is a gradual post-1.0 improvement, ratchet-guarded by `tests/api/test_raw_httpexception_guard.py` so the raw count cannot grow.

### 3. REST — 422 validation (canonical, since #992 Phase 1)

A `RequestValidationError` handler is registered in `backend/src/api/main.py` (`request_validation_exception_handler`, added in #992 Phase 1). Request-body validation failures now emit the canonical envelope:

```json
{
  "error": "VAL-001",
  "message": "Request validation failed",
  "details": {
    "errors": [
      { "loc": ["body", "field"], "msg": "...", "type": "..." }
    ]
  }
}
```

- HTTP 422, `error: "VAL-001"` — matches the `ValidationError` shape raised from service code, so the two 422 paths now agree.
- The handler projects each pydantic error to a trimmed `{loc, msg, type}` and **drops `input`/`ctx`/`url`** — the rejected payload is never echoed back (CWE-639 / info-disclosure defense for the frozen surface).

### 4. MCP tool errors (in-band, JSON-in-TextContent)

`_error_response` (`backend/src/mcp_server/tools/_helpers.py`) — every MCP tool error is a JSON-RPC **success** whose `CallToolResult` carries `isError: true` (#1622) and a single text item holding:

```json
{ "status": "error", "error": "<code>", "message": "<message>", "...extra fields" }
```

```json
{ "jsonrpc": "2.0", "id": "<request id>",
  "result": { "content": [{ "type": "text", "text": "{\"status\":\"error\",...}" }], "isError": true } }
```

This is the MCP *tool execution error* mechanism: the model gets the envelope and can self-correct, and clients (e.g. hook runners) see the failure without parsing the envelope. Success results carry no `isError` key. Both transports (legacy `initialize` sessions and stateless 2026-07-28) emit the same flag; the helper marks its return value and the transport reads it, so a hand-built `{"status": "error"}` payload in `mcp_server/tools/` fails `tests/mcp_server/test_error_envelopes.py`.

`MemoryCloudException` is caught in tool handlers and its `error_code`/`message`/`details` pass through verbatim (e.g. `backend/src/mcp_server/tools/resource.py:1162-1175`), so REST codes like `CONNECTOR-001` appear unchanged on the MCP surface.

**Unexpected failures (#1684).** The dispatch catch-alls in `execute_tool_call`, the per-tool `except Exception` arms and the transport fallbacks share one vocabulary, `backend/src/mcp_server/tools/_errors.py`:

- a *refusal* (`MemoryCloudException` below 500, a plain `ValueError`, a `PermissionError`) keeps its message under a stable code (`validation_error`, `permission_denied`, `not_found`, `conflict`, `quota_exceeded`, `rate_limit_exceeded`, `feature_not_available`) plus `help`; `AuthorizationError` / `AdminProtectionError` never forward `details`, and a `PermissionError`'s text is never returned. A builtin `ValueError` / `PermissionError` refusal is logged with its text and traceback. The handlers whose catch-all always returned a fixed message (analysis, `api_keys`, `agent_bootstrap`, `secrets`; #1247) pass `echo_value_error=False`, so a plain `ValueError` there is a server failure;
- anything else is a *server failure*: `error` is the tool's legacy `<tool>_error` code or else the `cause`; the envelope adds `cause` (`timeout` / `service_unavailable` / `internal_error`), `help`, `correlation_id` (the request's W3C trace id when set, else random), `retryable` (true for read-only tools and the repeat-safe writes `recall`, `get_agent_bootstrap` and `secret_register_pubkey`), `retry_after_seconds` (retryable calls, transient causes) and `outcome: "unknown"` (other writes). The exception text, type and traceback are logged (structured `mcp_tool_failed` event) under the `correlation_id` and never returned.

Before #1684 the dispatch catch-alls returned `{"status": "error", "error": "<str(e)>"}` with no `message`. Caller-facing contract and migration notes: `docs/mcp-tools.md` › Errors.

### 5. MCP transport — JSON-RPC protocol errors

Exceptions that escape `execute_tool_call` in `tools/call` map to a JSON-RPC error object (`backend/src/mcp_server/transport.py`), HTTP 200. In practice `execute_tool_call` catches handler exceptions itself and returns them as the in-band envelope above (with `isError: true`), so this path is reached only by transport-level failures:

```json
{
  "jsonrpc": "2.0", "id": "<request id>",
  "error": {
    "code": -32xxx,
    "message": "<fixed sentence from the #1684 vocabulary>",
    "data": { "error": "<code>", "help": "...", "cause": "...", "correlation_id": "...", "retryable": false, "outcome": "unknown" }
  }
}
```

`data` is the vocabulary of §4 (server-failure fields only for server failures). Before #1684 it was `{"exception_type": "<PyClassName>", "details": "<str(e), truncated 500>"}` and the `-32603` message was `Internal error: <str(e)>`; neither the exception's text nor its type is returned any more.

| JSON-RPC code | Trigger | Site |
|---|---|---|
| `-32001` | `cause: timeout` — tool execution timeout (custom) | `handle_streamable_http_post` — `tools/call` fallback |
| `-32002` | `data.error: permission_denied` (`PermissionError`, 401/403 `MemoryCloudException`) — permission denied (custom) | same |
| `-32602` | `data.error: validation_error` (a plain `ValueError`, or a 4xx `MemoryCloudException` with no more specific code) — invalid params (standard) | same |
| `-32603` | anything else — internal error (standard) | same |

Since #1684 the code is derived from the classification in `data`, not the Python type, so the two never disagree: a `ValueError` subclass (a server failure) gets `-32603`, an `httpx` timeout `-32001`.

Requests whose `method` the transport does not implement (anything other than `initialize` / `tools/list` / `tools/call` / `ping` / `server/discover`, e.g. `resources/list`) get a standard `-32601` **Method not found**, also HTTP 200 (the terminal branch of `handle_streamable_http_post`, #1541). Before #1541 this path dereferenced a `session.transport` attribute that no longer existed and was an unconditional HTTP 500.

| JSON-RPC code | Trigger | Site |
|---|---|---|
| `-32601` | unknown / unimplemented request method (standard) | `handle_streamable_http_post` — terminal branch |
| `-32602` | `tools/list` on a URL whose tool profile cannot be served — unknown `?profile=` value, or a `?tools=` allowlist matching no tool (#1601); no `data` | `handle_streamable_http_post` — `tools/list` branch |
| `-32600` | body is not a single JSON-RPC object (scalar / batch array), or a message without a string `method` — with or without an `id`, so a malformed id-less envelope is not mistaken for a notification — HTTP **400** (standard) | `handle_streamable_http_post` — envelope guards |

#### Modern (MCP 2026-07-28) requests — stateless path

The server is dual-era (#1544). The tables above describe the **legacy** half (`initialize` handshake, `Mcp-Session-Id`). A request that carries per-request `params._meta["io.modelcontextprotocol/protocolVersion"]` — and every `server/discover`, even a bare probe — is served statelessly by `handle_stateless_post` (`backend/src/mcp_server/transport_stateless.py`) — no session is minted, required, or echoed — and follows the 2026-07-28 error contract instead:

| HTTP | JSON-RPC code | Trigger |
|---|---|---|
| 400 | `-32600` | message without a string `method`, or a request `id` that is not a string / integer |
| 400 | `-32602` | `_meta.protocolVersion` not a string, `_meta.clientCapabilities` present but not an object; `tools/call` without a string `name`, or with `arguments` that is neither an object nor `null` (an explicit `null` is treated as omitted, like the reference SDK) |
| 400 | `-32602` | `tools/list` on a URL whose tool profile cannot be served — unknown `?profile=` value, or a `?tools=` allowlist matching no tool (#1601). The legacy path keeps HTTP **200** for the same code |
| 400 | `-32020` **HeaderMismatch** | `MCP-Protocol-Version`, `Mcp-Method` or (for `tools/call`) `Mcp-Name` header present but undecodable or different from the body value (`Mcp-Name` is Base64-sentinel-decoded first) |
| 400 | `-32022` **UnsupportedProtocolVersion** | requested version is not a modern revision this server serves — settled before every other rule; `data` = `{ "supported": [...], "requested": "..." }`. `supported` lists the legacy revisions too — they are reachable through `initialize` |
| 404 | `-32601` | unknown / unimplemented method (`resources/*`, `prompts/*`, `subscriptions/listen`, …). The legacy path keeps HTTP **200** for the same code |
| 200 | `-32602` | an exception escaping `execute_tool_call` that the #1684 vocabulary classifies as `validation_error` (a plain `ValueError`, or a 4xx `MemoryCloudException` with no more specific code) |
| 200 | `-32603` | any other exception escaping `execute_tool_call`. `message` and `data` come from the #1684 vocabulary (§4, §5) — the exception text is logged, not returned. The legacy-range `-32001` / `-32002` are **not** used on this path |

**Deliberate leniency.** The spec makes the mirrored headers and `_meta.clientCapabilities` MUSTs. Their *absence* is tolerated — the request is served and a warning naming the gaps is logged — because nothing here routes on those headers or relies on a client capability; only a header that *contradicts* the body is rejected. The `MCP-Protocol-Version` header is likewise not an era signal (legacy session clients send it too).

Successful results carry `resultType: "complete"` and `_meta["io.modelcontextprotocol/serverInfo"]`; `server/discover` and `tools/list` additionally carry the `ttlMs` / `cacheScope` caching hints.

⚠ The application `error_code` does **not** pass through this path — a `MemoryCloudException` falls into the `-32603` bucket and `data.error` carries its #1684 vocabulary code. (In practice `execute_tool_call` catches it first, path 4 above.)

### 6. MCP transport — OAuth 401 challenge (RFC 6750)

Auth failure before dispatch (`transport.py:531-580`): HTTP 401, body `{"error": "<code>", "error_description": "<text>"}` plus a `WWW-Authenticate: Bearer realm=..., error=..., error_description=..., resource_metadata=...` header. Codes are deliberately restricted to the RFC 6750 §3.1 vocabulary — internal `AUTH-xxx` codes must NOT leak into the challenge:

| Code | Trigger | Site |
|---|---|---|
| `invalid_request` | missing/malformed Bearer credentials (default) | `transport.py:543` |
| `invalid_token` | `TokenExpiredError` / `TokenRevokedError` / `InvalidTokenError` | `transport.py:547` |

---

## Error code catalogue

**Total: 60 distinct error code values** (54 distinct values matched by the `error_code` grep + `RES-004`, which is emitted inline as an `"error"` literal with no exception class — reserved per the comment at `exceptions.py:177-181` + `-32601` / `-32600`, emitted inline in the transport dispatch, #1541 + `BETA-INVITE-001` / `BETA-INVITE-002`, #1581 + `BETA-INVITE-003`, #1595). **60 of 60 enumerated** below. JSON-RPC numeric codes are counted (6 values); `invalid_token` is one value with three emitting classes plus the RFC 6750 path.

### A. `MemoryCloudException` hierarchy (REST canonical shape) — `backend/src/utils/exceptions.py`

| Code | Exception class (file:line of code assignment) | HTTP | Meaning |
|---|---|---|---|
| `AUTH-001` | `AuthenticationError` — exceptions.py:54 | 401 | Authentication failed (generic). |
| `AUTH-002` | `InvalidCredentialsError` — exceptions.py:64 | 401 | Invalid credentials. |
| `AUTH-003` | `TokenExpiredError` — exceptions.py:71 | 401 | Token expired. |
| `AUTH-101` | `AuthorizationError` — exceptions.py:92 | 403 | Insufficient permissions (uniform message; `details` always stripped, CWE-639). |
| `AUTH-201` | `APIKeyError` — exceptions.py:129 | 401 | Invalid or missing API key. |
| `AUTH-202` | `APIKeyRevokedError` — exceptions.py:139 | 401 | API key revoked. |
| `AUTH-203` | `APIKeyExpiredError` — exceptions.py:146 | 401 | API key expired. |
| `ADMIN-001` | `AdminProtectionError` — exceptions.py:117 | 403 | System-admin invariant blocks operation (initial/last admin); `details` always stripped. |
| `RES-001` | `NotFoundException` — exceptions.py:159 | 404 | Resource not found. |
| `RES-002` | `ConflictError` — exceptions.py:188 | 409 | Resource conflict. |
| `RES-003` | `MemoryGoneError` — exceptions.py:174; `BetaInviteGoneError` — exceptions.py:595 | 410 | Resource soft-deleted (distinct from 404 so clients stop retrying). Since #1581 also a closed-beta invite link that existed but is expired or already redeemed (`GET /beta-invites/{token}/preview`) — one message for both reasons. |
| `RES-004` | *(no class — inline `JSONResponse`)* — api/routes/attachments.py:30 | 410 | Deprecated `/api/v1/attachments/*` retired; carries Sunset/Deprecation/Link headers. |
| `VAL-001` | `ValidationError` — exceptions.py:195 | 422 | Service-layer validation error (shape/format). ⚠ Coexists with the non-conforming FastAPI 422. Since #1644 the shared-context refusal is **no longer** here — it moved to `FEAT-001` at 403, matching what the REST route already answered for the same condition. The managed-LLM refusal stays `VAL-001`/422 and gains a [gate annotation](#gate-refusals-1644) (`plan` when the deployment has a managed model, `deployment` when it has none). |
| `REQ-001` | `BadRequestError` (default) — exceptions.py:217 | 400 | State-precondition failure (call sites may override the code, e.g. `REQ-101`/`REQ-102`). |
| `MEDIA-001` | `UnsupportedMediaTypeError` — exceptions.py:254 | 415 | Content-Type not in allow-list; `details.allowed` lists accepted types. |
| `BONUS-001` | `InsufficientReasonError` — exceptions.py:283 | 400 | Slot-bonus shrink below owned count requires a reason. |
| `BONUS-002` | `BonusBelowZeroError` — exceptions.py:301 | 400 | Resulting workspace_slot_bonus would be negative. |
| `RATE-001` | `RateLimitError` — exceptions.py:318; also inline at api/middleware/rate_limit.py:124 | 429 | Per-minute rate limit exceeded; `Retry-After` + `X-RateLimit-*` headers on the middleware path. |
| `QUOTA-001` | `QuotaExceededError` — exceptions.py:339; also inline at api/middleware/rate_limit.py:156 | 429 (**403** on two caps) | Quota exceeded; `Retry-After: 86400` on the middleware path. Since #1644 every **typed** instance — one whose `quota_type` is in the [frozen vocabulary](#quota_type-vocabulary-frozen) — carries the [quota gate block](#gate-refusals-1644) (`gate: "quota"`, `quota_type`, and `current`/`limit` where counts exist). An untyped `QUOTA-001` (the 1 MB memory-size guard, the total memory cap, a missing workspace) is not a plan quota and carries **no** `gate` — see [Not every `QUOTA-001` is a gate](#not-every-quota-001-is-a-gate). ⚠ Two caps answer **403**, not 429, because they always have and clients branch on it: the resource-token cap (`POST /resource-tokens`) and — under its own `CONNECTOR-001` code — the connector seat cap. `QuotaExceededError` takes a `status_code` kwarg for exactly those two; everything else uses the 429 default. |
| `QUOTA-002` | `EmbeddingSpendCapExceeded` — exceptions.py:383 | 429 | BYOK embedding spend cap reached (`details.period` = daily/monthly). Carries `gate: "quota"` and `quota_type` (`embedding_spend_daily` / `embedding_spend_monthly`) but **no** `current`/`limit`: the cap is USD, and the canonical count pair is integers. `cap_usd` / `current_usd` remain the numbers to render. |
| `FEAT-001` | `FeatureNotAvailableError` — exceptions.py:602 | 403 | Feature not available on current plan tier. Since #1551 also the "may create" refusal for XL-only features — `POST /resource-tokens` (`details.feature="resources"`), connector provisioning (`"connectors"`, REST and MCP `setup_connector`, where MCP surfaces it under `plan_required` with `required_plan`), `PUT /contexts/{id}` with `is_public=true` and the bound public API-key mint (`"public_contexts"`). The message names the minimum tier from the plan registry (`feature_denied_message`). Existing objects on lower tiers are never refused (block-new-only). Since #1644 it also covers `team_invitations` (`POST /invitations`, previously a raw `HTTPException(403)`), `shared_contexts` (previously `VAL-001`/422 on the service path), `sleep_mode`, `managed_embeddings` (previously `CFG-001`/500) and `managed_llm`; every instance carries the [feature gate block](#gate-refusals-1644), and `details.gate` distinguishes a **plan** refusal from an **allowlist** (rollout kill switch) or **deployment** (operator switch) refusal — the three used to be wire-identical. |
| `DB-001` | `DatabaseError` — exceptions.py:406 | 500 | Database operation failed. |
| `DB-002` | `DatabaseConnectionError` — exceptions.py:416; also via `SQLAlchemyError`/connection-error handlers (api/main.py:342-403) | 503 | DB unavailable; `Retry-After: 5`. |
| `EXT-001` | `ExternalServiceError` (default) — exceptions.py:426; inherited by `QdrantError` (exceptions.py:434-438) and direct raises in storage/factory.py:47,65, storage/r2.py:102 | 502 | Generic external-service error. ⚠ Qdrant has no dedicated code; `EXT-101` is an unexplained gap before Redis's `EXT-102`. |
| `EXT-102` | `RedisError` — exceptions.py:445 | 502 | Redis service error. |
| `EXT-201` | `OpenAIError` — exceptions.py:452 | 502 | OpenAI API error (direct, non-fallback-chain). |
| `EXT-202` | `CohereError` — exceptions.py:459 | 502 | Cohere API error. |
| `EXT-203` | `VoyageError` — exceptions.py:469 | 502 | Voyage AI reranker error. |
| `EXT-204` | `StripeError` — exceptions.py:482 | 502 | Stripe API unexpected-shape error. |
| `EXT-205` | `EmailDispatchError` — exceptions.py:511 (status overridden to 503 at :515) | 503 | Email-provider dispatch failed; retriable. |
| `invalid_token` | `TokenRevokedError` — exceptions.py:526; `InvalidTokenError` — exceptions.py:535; RFC 6750 challenge — mcp_server/transport.py:547 | 401 | OAuth2 token revoked/invalid. ⚠ RFC 6750-mandated lowercase vocabulary, intentionally outside the `NAMESPACE-NNN` convention; on the REST path it surfaces in the canonical body as `"error": "invalid_token"`. |
| `CFG-001` | `ConfigurationError` — exceptions.py:546 | 500 | Server configuration error. Since #1644 this **no longer** includes the managed-embeddings plan refusal ("add a BYOK key, self-host a model, or upgrade"): a refusal that can never succeed on retry does not belong in the server-error class, so it answers `FEAT-001` at 403. A genuinely missing credential is still `CFG-001`. |
| `CFG-002` | `ConfigReadOnlyError` — exceptions.py:206 | 409 | #1580: `PUT /config/{key}` / `POST /config/batch` always refuse — every key is env-backed (set via environment, applied on restart/redeploy) and nothing reads a stored override. `details.keys` lists the refused keys; a batch is refused as a whole. |
| `INT-001` | `InternalError` — exceptions.py:553 | 500 | Internal server error (structured). |
| `ERASURE-001` | `ErasureRequestNotFoundError` — exceptions.py:564 | 404 | No erasure request found. |
| `ERASURE-002` | `ErasureTokenInvalidError` — exceptions.py:571 | 400 | Erasure confirmation token missing/expired/mismatched. |
| `ERASURE-003` | `ErasureForbiddenError` — exceptions.py:583 | 403 | Erasure not permitted (e.g. password mismatch). |
| `ERASURE-004` | `InitialAdminCannotBeErasedError` — exceptions.py:598 | 403 | Initial system admin is a protected account. |
| `ERASURE-005` | `WorkspaceTransferRequiredError` — exceptions.py:617 | 409 | Must transfer workspace ownership before erasure. |
| `ERASURE-006` | `ErasureAlreadyInProgressError` — exceptions.py:630 | 409 | Erasure request already pending/in progress. |
| `BETA-INVITE-001` | `BetaInviteQuotaExceededError` — exceptions.py:552 | 409 | #1581: `POST /beta-invites` at the per-user cap. Since #1595 also `POST /beta-invites/{id}/reissue` of an `expired` invite at the cap (the invite is left untouched). `details.reason` = `"quota_exceeded"` (stable discriminator), `details.quota` = the cap. |
| `BETA-INVITE-002` | `BetaInviteAlreadyRedeemedError` — exceptions.py:565 | 409 | #1581: `DELETE /beta-invites/{id}` — and since #1595 `POST /beta-invites/{id}/reissue` — on an invite that was already used. `details.reason` = `"already_redeemed"`. |
| `BETA-INVITE-003` | `BetaInviteAlreadyRevokedError` — exceptions.py:579 | 409 | #1595: `POST /beta-invites/{id}/reissue` on an invite that was already revoked (including the second request of a double-click — nothing is minted). `DELETE` stays idempotent and never emits it. `details.reason` = `"already_revoked"`. |

### B. Service/route-level codes (raised as `MemoryCloudException`/subclass with overridden code)

| Code | Call site | HTTP | Meaning |
|---|---|---|---|
| `CONNECTOR-001` | `MemoryCloudException` — services/connector_provisioning.py:407 | 403 | Connector seat limit reached for plan. Code and status are unchanged by #1644; the refusal gains the [quota gate block](#gate-refusals-1644) with `quota_type: "connectors"`, so a client reads it the same way it reads every other cap. `max_connectors` / `active_connectors` stay beside the canonical `limit` / `current`. |
| `CONNECTOR-002` | `MemoryCloudException` — services/connector_provisioning.py:503 | 503 | Connector seat lock unavailable (PG 55P03 lock timeout); retry. |
| `EXT-ANA-001` | `ExternalServiceError` subclass — services/analysis/llm_caller.py:83 | 502 | Entire OpenAI fallback chain exhausted (`details.upstream_provider_error=true`, `attempted_models`). |
| `REQ-101` | `BadRequestError` — services/system_admin_service.py:111 | 400 | User is already a system admin. ✅ **#992 Phase 2**: re-namespaced from `ADMIN-101` to the `REQ-*` (BadRequestError) family — resolves the `ADMIN-001` (403 protection) vs `ADMIN-1xx` (400 precondition) collision; `ADMIN-*` now uniformly means admin-protection. |
| `REQ-102` | `BadRequestError` — services/system_admin_service.py:184 | 400 | User is not a system admin. ✅ **#992 Phase 2**: re-namespaced from `ADMIN-102` → `REQ-102` (same rationale). |
| `SLEEP-001` | `MemoryCloudException` — api/routes/admin_sleep.py:234 | 500 | Admin user id missing from session (defensive). ✅ **#992 Phase 2**: renamed from snake_case `admin_user_id_missing` to the `NAMESPACE-NNN` convention. |
| `SLEEP-002` | `MemoryCloudException` — api/routes/admin_sleep.py:255 | 409 | A sleep run is already in progress for this user. ✅ **#992 Phase 2**: renamed from `sleep_run_in_progress`. |
| `SLEEP-003` | `MemoryCloudException` — api/routes/admin_sleep.py:278 | 404 | No eligible contexts for sleep maintenance. ✅ **#992 Phase 2**: renamed from `sleep_target_not_found`. |

### C. MCP-only codes assigned via `error_code` variable — `backend/src/mcp_server/tools/resource.py`

Emitted in the MCP tool envelope (`{"status":"error","error":...}`); the logged HTTP-equivalent status is shown.

| Code | Call site | HTTP-equiv | Meaning |
|---|---|---|---|
| `resource_id_conflict` | resource.py:1026 | 409 | `resource_id` already in use in this context. |
| `context_name_conflict` | resource.py:1029 | 409 | Context name already exists in this workspace. |
| `conflict` | resource.py:1032 | 409 | Other uniqueness-constraint violation (sanitized). |

### D. JSON-RPC protocol codes — `backend/src/mcp_server/transport.py`

| Code | Site | Meaning |
|---|---|---|
| `-32001` | transport.py:246 | Tool execution timeout (custom). |
| `-32002` | transport.py:249 | Permission denied (custom). |
| `-32602` | transport.py:252 | Invalid params (standard). |
| `-32603` | transport.py:255 | Internal error (standard; catch-all — application `error_code` is NOT propagated here ⚠). |
| `-32601` | transport.py `handle_streamable_http_post` (terminal branch) | Method not found (standard) — any request method the transport does not implement (#1541). |
| `-32600` | transport.py `handle_streamable_http_post` (envelope guards) | Invalid Request (standard) — non-object / batch body, or missing `method` (checked before the notification 202, so it applies to id-less envelopes too); HTTP 400 (#1541). |

### E. RFC 6750 challenge codes — `backend/src/mcp_server/transport.py`

| Code | Site | Meaning |
|---|---|---|
| `invalid_request` | transport.py:543 | Missing/malformed Bearer credentials (default). |
| `invalid_token` | transport.py:547 | Token expired/revoked/invalid (also catalogued in §A). |

### Supplementary: MCP `_error_response` literal codes (outside the `error_code` grep, same `"error"` field)

These 41 distinct snake_case literals are passed directly as the first argument to `_error_response(...)` across `backend/src/mcp_server/tools/*.py` and `mcp_server` dispatch — they occupy the same `"error"` field on the MCP surface and are listed for surface completeness (occurrence counts from `grep -rhoP '_error_response\(\s*"[^"]+"'`):

`internal_error` (37), `validation_error` (29), `missing_fields` (20), `workspace_required` (13), `invalid_context_id_format` (5), `invalid_report_id` (3), `invalid_memory_id_format` (3), `db_error` (3), `workspace_not_found` (2), `permission_denied` (2), `not_found` (2), `invalid_arguments` (2), and one each of: `update_search_config_error`, `update_context_error`, `unknown_tool`, `setup_resource_error`, `setup_connector_error`, `service_unavailable`, `quota_exceeded`, `missing_required_fields`, `merge_contexts_error`, `list_tags_error`, `list_resource_tokens_error`, `list_my_bindings_error`, `list_analyses_error`, `invalid_search_config`, `invalid_context_id`, `invalid_argument`, `ingest_events_error`, `get_usage_error`, `get_resource_schema_error`, `get_resource_impact_error`, `get_cluster_error`, `get_analysis_error`, `get_active_analysis_error`, `describe_binding_error`, `delete_context_error`, `create_context_error`, `conflict` (also §C), `binding_not_found`, `analyze_context_error`.

⚠ `invalid_argument` vs `invalid_arguments`, and `missing_fields` vs `missing_required_fields`, are accidental near-duplicates.

Since #1684 the per-tool `<tool>_error` codes of the unexpected-failure arms are passed as `error=` to `_errors._tool_exception_response(...)`, which the literal grep above does not see, and `_errors` also emits `timeout`, `service_unavailable`, `internal_error`, `validation_error`, `permission_denied`, `not_found`, `conflict`, `quota_exceeded`, `rate_limit_exceeded` and `feature_not_available` for exceptions that reach it. The file tools' `service_unavailable` literal above is gone: their storage failures now reach the dispatch catch-all and `_errors`.

---

## Gate refusals (#1644)

A **gate refusal** is a refusal caused by what the caller's workspace is allowed to do —
its plan, a quota, a rollout allowlist or an operator switch — rather than by the shape of
the request. Since #1644 every one of them carries a machine-readable `details` block, so a
client never has to match on prose or infer a reason from a bare status code.

`details.gate` is an **additive annotation, orthogonal to `error` and `status`**. Nothing was
renamed and nothing was removed: an older client that ignores the new keys sees the same
status, the same message and the same pre-existing detail fields it always did.

### The `gate` vocabulary

| `gate` | Meaning | Carries an upgrade path? |
|---|---|---|
| `plan` | The workspace's tier does not include the feature, or its cap was reached. | Yes — `required_plan` when a tier lifts it. |
| `quota` | A cap was reached. | Only when a higher tier raises that cap. |
| `allowlist` | A rollout kill switch. Plan-neutral: no tier turns it on. | Never. |
| `deployment` | The operator disabled the feature on this deployment. | Never. |
| `role` | Vocabulary member only — **never serialized**. Role refusals are `AUTH-101` and have their `details` stripped wholesale (CWE-639 defence in depth); a client identifies them by the code. | — |

### `FEAT-001` (403) — feature gate

```jsonc
{
  "error": "FEAT-001",
  "message": "Feature 'team_invitations' not available on M plan. Upgrade to L plan to access this feature.",
  "details": {
    "gate": "plan",                  // "plan" | "allowlist" | "deployment"
    "feature": "team_invitations",   // registry key
    "required_plan": "pro",          // nullable — PLAN_ORDER key
    "required_plan_display": "L",    // nullable — the tier's display_name
    "current_plan": "basic"          // nullable
  }
}
```

| Field | Type | Required | `null` when |
|---|---|---|---|
| `gate` | `"plan" \| "allowlist" \| "deployment"` | yes | — |
| `feature` | `string` | yes | — |
| `required_plan` | `string \| null` | no | no tier carries the feature (a `PLAN_<KEY>_FEATURES` override can strip it); always absent or `null` for `gate != "plan"` |
| `required_plan_display` | `string \| null` | no | mirrors `required_plan`. **Never** the `"higher"` prose fallback that `required_plan_display_name` returns for the message — that is a sentence fragment, not a label |
| `current_plan` | `string \| null` | no | the caller had no workspace row |

Both halves ship on purpose: `required_plan` is the **key** a UI decides with (display labels
are per-deployment and per-locale, and the server cannot know the browser's locale), while
`required_plan_display` is the **label** a CLI or SDK consumer renders, having no tier matrix
of its own.

### `QUOTA-001` / `QUOTA-002` / `CONNECTOR-001` — quota gate

```jsonc
{
  "error": "QUOTA-001",
  "message": "Context limit reached. Your S plan allows 1 context(s) per workspace. Upgrade to M plan for more contexts.",
  "details": {
    "gate": "quota",
    "quota_type": "contexts",        // frozen vocabulary, below
    "current": 1,                    // int
    "limit": 1,                      // int
    "required_plan": "basic",        // nullable
    "required_plan_display": "M",    // nullable
    "current_plan": "free",          // nullable
    "feature": null,                 // set when the cap belongs to a named feature
    "resets_at": null                // ISO-8601, time-windowed quotas only
  }
}
```

`current` / `limit` are **added alongside** every pre-existing field, never replacing it. The
legacy names that keep shipping, per refusal:

| Refusal | Pre-existing keys that stay | Canonical aliases added |
|---|---|---|
| Analysis daily quota | `used_today`, `limit_today`, `addon_bonus`, `remaining_today`, `resets_at` | `current`, `limit` |
| Memories per day | `limit`, `used_today`, `requested`, `resets_at` | `current` |
| Workspace cap | `owned_count`, `cap`, `tier`, `next_tier` | `current`, `limit` |
| Storage bytes | `limit`, `current`, `requested` | already canonical |
| Sleep-enabled contexts | `limit`, `current`, `addon_bonus`, `requested` | already canonical |
| Connector seats (`CONNECTOR-001`) | `max_connectors`, `active_connectors` | `current`, `limit` |
| Embedding spend (`QUOTA-002`) | `period`, `cap_usd`, `current_usd` | none — the cap is USD |
| Daily API quotas (rate-limit middleware) | `retry_after` on the 429 | none — a rolling counter, no cap pair to render |

**Two families therefore carry no numbers**: `QUOTA-002` and the rate-limit family. They are
still `gate: "quota"`, and a client renders a number-free message for them rather than
interpolating a `limit` it was never sent.

### Not every `QUOTA-001` is a gate

`QuotaExceededError` is also raised for limits no tier lifts: the 1 MB memory-size guard on
remember / update / patch, the total memory cap, and the "workspace not found" anomalies. They
answer `QUOTA-001` / 429 as they always have, but with `details: {"quota_type": null}` and no
`gate`: the server stamps `gate: "quota"` only on a refusal whose `quota_type` is in the frozen
vocabulary below, so an untyped raise cannot be read as an upsell. A client falling back to the
error code (a server predating #1644 sends no `gate`) applies the same rule — `QUOTA-001` is a
quota gate only when it names a frozen `quota_type`; counts alone do not qualify. `QUOTA-002`
and `CONNECTOR-001` each name exactly one cap, so their code is enough.

### MCP envelopes carry the same block

On the MCP surface a gate refusal arrives as the in-band tool error (§4). The mappers that see
one forward the same keys the REST `details` carries — `gate`, `feature`, `required_plan`,
`required_plan_display`, `current_plan`, and on a quota `quota_type` / `current` / `limit` /
`resets_at` — as top-level envelope fields: `feature_not_available` and `quota_exceeded` from
the analysis tools; `plan_required` from `create_context`, `update_context` (`is_public`),
`setup_resource` and `setup_connector` (whose seat cap keeps its `CONNECTOR-001` code);
`quota_exceeded` from `remember`, `init_file_upload`, `register_agent`, and the context and
token caps of `create_context` / `setup_resource`. Every envelope keeps the `error` code and
the fields it carried before (`required_plan`, `help`, the legacy counts); the gate keys are
added beside them. The `quota_exceeded` envelopes omit `null` values, which a client reads the
same as `null`. `test_gate_error_contract.py` lists which refusals reach an MCP tool and why the
rest do not.

### `quota_type` vocabulary (frozen)

`contexts`, `members`, `workspace_limit_reached`, `memories_per_day`, `memory_analysis`,
`sleep_enabled_contexts`, `storage_bytes`, `agents`, `resource_tokens`, `connectors`,
`embedding_spend_daily`, `embedding_spend_monthly`, `api_mcp_daily`, `api_rest_daily`,
`api_public_daily`.

Frozen in `backend/src/config/constants.py` and enumerated by
`backend/tests/api/test_gate_error_contract.py`, which fails if a refusal emits a type outside
this set, if a type here has no live refusal, or if a new refusal site is added without
declaring which vocabulary it belongs to. `workspace_limit_reached` reads like prose but is
kept verbatim: it is already on the wire and renaming it would break existing clients.

### Status codes

Codes and statuses do not move for the sake of the annotation. #1644 moved exactly two, each
as its own revertable commit:

| Refusal | Was | Now | Why |
|---|---|---|---|
| Shared contexts, service path | `VAL-001` / 422 | `FEAT-001` / 403 | The REST route already answered `FEAT-001`/403 for the identical condition; one condition was producing two different refusals depending on which door the caller came through. |
| Managed embeddings | `CFG-001` / 500 | `FEAT-001` / 403 | A plan refusal can never succeed on retry; answering 5xx polluted error-rate SLOs and invited clients to retry. |

The resource-token cap keeps **403** and becomes `QUOTA-001`; the connector seat cap keeps
**403** under `CONNECTOR-001`. Unifying those two with the 429 default is a follow-up.


---

## Non-conforming emissions

Raw `raise HTTPException(` statements bypass the canonical `{error, message, details}` shape and emit FastAPI's `{"detail": ...}` instead — no machine-routable code.

**Count: 369 grep matches across 42 files; 3 matches are docstring examples only (`api/middleware/session.py:19,154`, `utils/error_messages.py:8`), leaving 366 real raise sites in 40 files.** Full per-file table (grep-match counts; the 3 docstring-only matches are marked):

| Count | File |
|---|---|
| 31 | backend/src/api/routes/oauth.py |
| 29 | backend/src/api/routes/member_credentials.py |
| 29 | backend/src/api/routes/contexts.py |
| 24 | backend/src/api/routes/auth.py |
| 19 | backend/src/api/routes/admin.py |
| 18 | backend/src/auth/dependencies.py |
| 17 | backend/src/api/routes/resource_tokens.py |
| 12 | backend/src/api/routes/workspace_connectors.py |
| 12 | backend/src/api/routes/external_keys.py |
| 12 | backend/src/api/routes/connectors_slack.py |
| 11 | backend/src/api/routes/api_keys.py |
| 10 | backend/src/api/routes/memory.py |
| 10 | backend/src/api/routes/invitations.py |
| 9 | backend/src/api/routes/workspace.py |
| 9 | backend/src/api/routes/resource_ingest.py |
| 9 | backend/src/api/routes/me_oauth.py |
| 9 | backend/src/api/routes/admin_plans.py |
| 8 | backend/src/plugins/billing/routes.py |
| 8 | backend/src/api/routes/public_search.py |
| 8 | backend/src/api/routes/analyses.py |
| 6 | backend/src/api/routes/workspace_plan.py |
| 6 | backend/src/api/routes/bm25_drift.py |
| 5 | backend/src/api/routes/workspaces.py |
| 5 | backend/src/api/routes/users.py |
| 5 | backend/src/api/routes/neural_config.py |
| 5 | backend/src/api/routes/cost_aggregation.py |
| 5 | backend/src/api/routes/context_search_config.py |
| 5 | backend/src/api/routes/config.py |
| 4 | backend/src/utils/auth_helpers.py |
| 4 | backend/src/auth/analysis_gates.py |
| 4 | backend/src/api/routes/workers.py |
| 4 | backend/src/api/routes/agent_state.py |
| 4 | backend/src/api/routes/admin_signup_gate.py |
| 3 | backend/src/api/routes/sleep_reports.py |
| 2 | backend/src/api/routes/system.py |
| 2 | backend/src/api/middleware/session.py *(docstring examples only — not emitted)* |
| 1 | backend/src/utils/error_messages.py *(docstring example only — not emitted)* |
| 1 | backend/src/utils/db_helpers.py |
| 1 | backend/src/api/routes/resources.py |
| 1 | backend/src/api/routes/me_account.py |
| 1 | backend/src/api/routes/feedback.py |
| 1 | backend/src/api/routes/admin_neural.py |

Representative sites (4 of 366 listed; full per-file counts above):

- `backend/src/utils/db_helpers.py:79` — generic `handle_db_operation` helper converts *any* exception to `HTTPException(500, detail=...)`, institutionalizing the non-canonical shape for every caller. ⚠
- `backend/src/auth/dependencies.py` (18 sites) — auth-dependency 401/403s in `{"detail": ...}` shape, sitting beside the canonical `AUTH-xxx` family. ⚠
- `backend/src/api/routes/oauth.py` (31 sites) — largest single-file cluster.
- `backend/src/utils/error_messages.py` — `ErrorMessages` constants module explicitly documents the `raise HTTPException(400, ErrorMessages.X)` pattern as the recommended usage. ⚠

Other non-conforming shapes (not raw HTTPException):

1. **FastAPI 422** — `{"detail": [ ... ]}` array shape; no `RequestValidationError` handler exists (§Canonical shape, point 3). ⚠
2. **MCP `-32603` catch-all** — swallows `MemoryCloudException.error_code` when a tool handler lacks its own catch (§Canonical shape, point 5). ⚠
3. ✅ **RESOLVED (#992 Phase 2)**: the three REST snake_case codes were renamed to `SLEEP-001/002/003` (§Catalogue B); the REST surface now uses `NAMESPACE-NNN` uniformly. (The 41 MCP `_error_response` snake_case literals at the section above are a separate MCP-surface concern, out of #992's REST scope.)

---

## Follow-up candidates

Issue acceptance allows at most 2 sub-issues; candidates are grouped into 2 bundles accordingly.

### Bundle 1 — Code-vocabulary normalization before freeze (mostly P1)

| Priority | Item | Feeds from |
|---|---|---|
| ✅ DONE (#992 Phase 2) | Renamed the 3 snake_case REST codes in `api/routes/admin_sleep.py` → `SLEEP-001/002/003` (tests + OpenAPI example + the frontend mock updated). | §B |
| ✅ DONE (#992 Phase 2) | Resolved the `ADMIN-*` collision by re-namespacing the 400 preconditions `ADMIN-101/102` → `REQ-101/102` (the BadRequestError family). `ADMIN-*` now uniformly means admin-protection (403). | §B |
| ✅ DONE (#992 Phase 1) | ~~Decide the 422 story~~ — **resolved**: a `RequestValidationError` handler now wraps FastAPI validation errors in the canonical envelope under `error: "VAL-001"` (`{loc, msg, type}` projection, `input` stripped). The two 422 shapes now agree. | §Canonical 3 |
| P2 | Give Qdrant a dedicated code (the `EXT-101` gap before Redis `EXT-102` strongly suggests it was reserved for Qdrant); keep `EXT-001` as the generic fallback only. | §A ⚠ |
| P2 | Remove the class-name fallback in `MemoryCloudException.__init__` (`error_code or self.__class__.__name__`) or make `error_code` required — the fallback can silently mint undocumented codes. | §Canonical 1 ⚠ |
| P2 | De-duplicate MCP literals `invalid_argument`/`invalid_arguments` and `missing_fields`/`missing_required_fields`. | §Supplementary ⚠ |
| P2 | Document (do not rename) the intentional RFC 6750 lowercase vocabulary (`invalid_token`, `invalid_request`) as a frozen exception to the convention. | §A, §E ⚠ |

### Bundle 2 — Eliminate `{"detail": ...}` emissions (continuation of #401/#602/#603/#604)

| Priority | Item | Feeds from |
|---|---|---|
| ✅ SHAPE DONE (#992 Phase 1+2) | **Route-level 401/403 (Phase 1)**: the 21 raw sites in `auth.py`, `oauth.py`, `public_search.py`, `workspace.py`, `member_credentials.py`, `admin.py` raise the canonical `AUTH-*`/`ADMIN-001` family with semantic codes. **Dependency-layer emitters** `auth/dependencies.py`, `auth/analysis_gates.py`, `utils/auth_helpers.py` are still raw `HTTPException` but their **shape is now canonical** via the Phase 2 global handler (placeholder `HTTP-<status>` code); their `WWW-Authenticate: Bearer` headers (`auth/dependencies.py:505/521`, `oauth.py` Bearer challenges) are preserved by the handler's `exc.headers` passthrough. Assigning these dependency-layer emitters semantic `AUTH-*` codes is the post-1.0 gradual follow-up. | §Non-conforming |
| P1 | Retire `utils/db_helpers.py:79` (`handle_db_operation` → `HTTPException(500)`) in favor of `DatabaseError`/`InternalError`, and stop `utils/error_messages.py` documenting the raw-raise pattern. | §Non-conforming ⚠ |
| ✅ DONE (#992 Phase 2) | Registered the global `StarletteHTTPException` stopgap handler that re-shapes `{"detail": ...}` → `{error: "HTTP-<status>", message, details: {}}`, freezing the *shape* of all ~296 remaining raw raises in one place (the chosen alternative — full per-site migration was out of scope for the pre-1.0 window). Assigning each placeholder `HTTP-<status>` a semantic `NAMESPACE-NNN` code (largest files first: `contexts.py` 29, `oauth.py` 26, `resource_tokens.py` 17, `auth.py` 17, `member_credentials.py` 15, `admin.py` 15) is the post-1.0 gradual follow-up, ratchet-guarded by `tests/api/test_raw_httpexception_guard.py`. | §Non-conforming |
| P2 | Propagate `error_code` through the MCP `-32603` catch-all via `error.data` (e.g. `data.error_code`) so transport-level failures stay SDK-routable. | §D ⚠ |
