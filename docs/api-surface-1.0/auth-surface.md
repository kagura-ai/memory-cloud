# Auth / OAuth / DCR Surface — pre-1.0 surface enumeration

> Issue: #622 — pre-1.0 public API surface enumeration and freeze
> Enumerated at commit 20ae959a2c79cacd2cf7922512ad780f540e9c60 (main HEAD, 2026-06-12)
> Re-frozen after #993 (dead JWT path dropped; well-known metadata honesty fixes)

Sources read in full: `backend/src/auth/mcp_scopes.py`,
`backend/src/api/routes/well_known.py`, `backend/src/auth/oauth2_bearer.py`,
`backend/src/auth/dependencies.py` (scope-enforcement sections),
`backend/src/auth/oauth2_server.py` (token-generation/grant sections),
`backend/src/api/routes/oauth.py` (route decorators + DCR/token/introspect handlers),
`backend/src/mcp_server/auth.py`. Enumeration is complete for the items below; no
silent truncation.

---

## JWT claim shapes

The platform issues **no JWT tokens**. Every credential (OAuth2 access/refresh
tokens, API keys, resource tokens, browser sessions) is an **opaque random string**
— they carry no claims; their attributes live in DB rows. The dashboard JWT module
that previously lived at `auth/jwt.py` was dead code and was removed in #993 (§1).

### 1. Dashboard JWT access token — REMOVED (#993)

The `auth/jwt.py` module (`create_access_token` / `verify_access_token` /
`decode_token_without_verification`) had **zero production callers** (and zero test
references) and was **deleted in #993** rather than frozen into the 1.0 surface. The
live dashboard auth is an opaque session cookie (`auth/session.py`,
`secrets.token_urlsafe(32)` session id resolved by SessionMiddleware) — not a JWT.
No claim shape is frozen here.

### 2. OAuth2 access token (authorization_code / refresh_token / device_code grants)

Opaque `secrets.token_urlsafe(32)` (43 chars, no prefix) — `auth/oauth2_server.py:_generate_token_with_expiry`.
No JWT claims. Server-side row (`oauth_tokens`) holds user_id, client_id, scope,
issued_at, expiry (3600 s for all three grants), revocation flags, and `resource`
(RFC 8707). The externally visible "claim surface" is the RFC 7662 introspection
response (7 fields, `models/schemas.py:TokenIntrospectionResponse`):

| Field | Type | Meaning | Presence |
|---|---|---|---|
| `active` | bool | Token validity | always |
| `client_id` | string | Issuing client | only when active |
| `scope` | string | Space-separated granted scopes; may be null for legacy tokens issued without scope | conditional |
| `exp` | int (epoch) | Expiry | only when active |
| `iat` | int (epoch) | Issued-at | only when active |
| `token_type` | string | Always `"Bearer"` | only when active |
| `aud` | string | RFC 8707 resource indicator; null if not requested | conditional |

### 3. OAuth2 refresh token

Opaque `secrets.token_urlsafe(32)`, issued alongside every access token
(unconditionally — see `offline_access` flag below). Long-lived, no automatic
expiration; rotated on use (old access+refresh revoked, RFC 6819 §5.2.2.3).

### 4. Device-flow token response extras (RFC 8628)

`DeviceAuthorizationGrant.generate_token` returns the standard 5 token-response
keys (`token_type`, `access_token`, `expires_in`, `scope`, `refresh_token`) **plus
non-standard identity fields** for SDK display:

| Field | Presence |
|---|---|
| `user_email` | conditional (when user row resolvable) |
| `workspace_id` | conditional (user has a non-deleted current workspace) |
| `workspace_name` | conditional (same) |

Code comments mark these as point-in-time and explicitly **not security-bearing**.
⚠ Non-standard members of an RFC 6749 token response — stability must be declared
(or namespaced) before freeze.

### 5. API keys and resource tokens (not JWTs, listed for completeness)

- API key: `kagura_` + `token_urlsafe(32)` (`auth/api_keys.py`, `API_KEY_PREFIX`); SHA-256 hash stored. Variants: global, workspace-scoped, public-bound (`bound_context_id`, #626 — rejected outside `/api/v1/public/*`).
- Resource token: `kagura_resource_` + `token_urlsafe(32)` (`auth/resource_tokens.py`); capability-typed, resource+workspace-scoped, hourly event quota.
- Bearer disambiguation contract (`oauth2_bearer.is_oauth_bearer_token`): anything **not** starting with `kagura_` is treated as an OAuth token. The `kagura_` prefix is therefore itself frozen surface.

---

## well-known metadata endpoints

Router: `backend/src/api/routes/well_known.py`, mounted at prefix `/.well-known`
(`api/main.py:588`). 4 endpoints total. `base_url` = `FRONTEND_URL` env;
`mcp_base` = `FRONTEND_URL` + `MCP_BASE_PATH` (default `/mcp`).

### 1. `GET /.well-known/oauth-protected-resource/mcp`

301 redirect → `/.well-known/oauth-protected-resource`. No body fields.

### 2. `GET /.well-known/oauth-protected-resource` (RFC 9728) — 6 fields

| Field | Value |
|---|---|
| `resource` | `{mcp_base}` (MCP canonical URL) |
| `authorization_servers` | `[{base_url}]` |
| `scopes_supported` | `ALL_ADVERTISED_SCOPES` (6 scopes, see catalogue) |
| `bearer_methods_supported` | `["header"]` |
| `resource_documentation` | `{base_url}/redoc` |
| `mcp_sse_endpoint` | `{mcp_base}/sse` |

✓ `resource_signing_alg_values_supported` and `resource_policy_uri` were **removed
in #993**: access tokens are opaque (no signing alg to advertise — a strict client
would attempt a doomed signature check) and there is no published policy document
(the old value pointed at the Swagger UI `/docs`). Both are OPTIONAL per RFC 9728.
✓ `mcp_sse_endpoint` is retained as a **stable, documented Kagura extension**
(non-RFC) — the MCP SSE transport entrypoint for Claude Desktop / Claude Code, a
committed part of the 1.0 surface.

### 3. `GET /.well-known/openid-configuration` (OIDC Discovery) — 15 fields

| Field | Value |
|---|---|
| `issuer` | `{base_url}` |
| `authorization_endpoint` | `{base_url}/api/v1/oauth/authorize` |
| `token_endpoint` | `{base_url}/api/v1/oauth/token` |
| `revocation_endpoint` | `{base_url}/api/v1/oauth/revoke` |
| `introspection_endpoint` | `{base_url}/api/v1/oauth/introspect` |
| `registration_endpoint` | `{base_url}/api/v1/oauth/register` |
| `device_authorization_endpoint` | `{base_url}/api/v1/oauth/device/authorize` (RFC 8628 §4; added #993) |
| `code_challenge_methods_supported` | `["S256"]` |
| `scopes_supported` | `ALL_ADVERTISED_SCOPES` (6) |
| `response_types_supported` | `["code"]` |
| `grant_types_supported` | `["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:device_code"]` |
| `token_endpoint_auth_methods_supported` | `["none", "client_secret_post", "client_secret_basic"]` |
| `introspection_endpoint_auth_methods_supported` | `["none"]` |
| `response_modes_supported` | `["query"]` |
| `subject_types_supported` | `["public"]` |

⚠ Missing OIDC-Discovery-REQUIRED fields `jwks_uri`, `userinfo_endpoint`,
`id_token_signing_alg_values_supported` — and the server never issues an
`id_token`. This is the #608 D5 "OIDC dishonesty", still open at this commit.
✓ `grant_types_supported` now advertises `urn:ietf:params:oauth:grant-type:device_code`
and the doc adds `device_authorization_endpoint` (#993, RFC 8628 §4). The device grant
is registered and live, and a `kagura-cli` public client is seeded for it (#624/#627);
the advertised set now matches the real surface.

### 4. `GET /.well-known/oauth-authorization-server` (RFC 8414) — 13 fields

Identical to openid-configuration **minus** `response_modes_supported` and
`subject_types_supported` (the other fields and values match exactly), including the
`device_authorization_endpoint` and device_code grant added in #993.

---

## OAuth scope catalogue

Single source of truth: `backend/src/auth/mcp_scopes.py:ALL_ADVERTISED_SCOPES`
(6 scopes; all four metadata advertisements derive from it since #592).

| Scope | Meaning | Advertised | Notes |
|---|---|---|---|
| `openid` | OIDC discovery compatibility only | yes (all sources) | Server issues no `id_token`; advertised so strict clients pass discovery (#608 D5, deferred) |
| `memory:read` | Read access to memory APIs | yes | REST: method-mapped to GET/HEAD/OPTIONS on `/api/v1/*` (`auth/dependencies.py`; 403 + `WWW-Authenticate: Bearer error="insufficient_scope"` on miss) |
| `memory:write` | Mutating access | yes | REST: POST/PUT/PATCH/DELETE; fail-closed default for unknown verbs |
| `memory:admin` | Intended: workspace-admin operations (#608 D3) | yes | Excluded from scope hierarchy (`memory:admin` does NOT imply `memory:write`); D3 route gates pending |
| `memory:delete` | Intended: distinct delete capability (#608 D4) | yes | D4 pending; HTTP DELETE currently maps to `memory:write` |
| `offline_access` | RFC 6749 refresh-token request | yes | Refresh-token issuance policy under pre-1.0 review |

**Enforcement matrix:** the per-scope × per-transport enforcement matrix is
being completed as part of pre-1.0 hardening; it is tracked in the internal
hardening ledger and this section will be re-frozen with the final matrix
before v1.0.0-rc1.

API keys and resource tokens are outside the scope system by design (role/RBAC
and capability lanes respectively, per #608 D2).

---

## OAuth / DCR endpoints

Router: `backend/src/api/routes/oauth.py`, `APIRouter(prefix="/oauth")` mounted
under `/api/v1` → all paths below are `/api/v1/oauth/...`. 19 routes total
(paths + grant types only, per enumeration scope).

Semver-locked discovery-referenced endpoints:

| Path | Method | Purpose |
|---|---|---|
| `/register` | POST | DCR (RFC 7591). Public, no auth. Provider whitelist (`chatgpt`/`claude`/`cursor` via redirect-URI/client-name detection incl. RFC 8252 loopback), 5 req/min/IP rate limit. Always forces `token_endpoint_auth_method="none"`; never returns `client_secret` (#689) |
| `/authorize` | GET | Authorization endpoint (consent page, HTML) |
| `/authorize` | POST | Consent decision submit |
| `/token` (canonical, schema-visible), `/token/` (hidden alias) | POST | Token endpoint (Authlib). Grants registered: `authorization_code` (PKCE S256, 10-min single-use codes), `refresh_token` (rotation), `urn:ietf:params:oauth:grant-type:device_code` (RFC 8628, #536) |
| `/revoke` | POST | RFC 7009 revocation (access or refresh; `token_type_hint` accepted) |
| `/introspect` | POST | RFC 7662 introspection; auth method `none` (public) |

Device flow (RFC 8628):

| Path | Method | Purpose |
|---|---|---|
| `/device/authorize` | POST | Device authorization request (device_code + user_code) |
| `/device/verify` | POST | Verify a user_code (browser side) |
| `/device/confirm` | POST | User confirms/denies the device grant |
| `/device/audit-unauth` | POST | Internal frontend audit beacon (#779); `include_in_schema=False`. Classified **documented-internal** (#993) — intentionally excluded from the public 1.0 surface |

Client-management (session-authenticated dashboard surface, not part of the
RFC-discovery contract but same router/prefix):

| Path | Methods |
|---|---|
| `/clients` | GET (list), POST (create, returns secret once) |
| `/clients/{client_id}` | GET, PUT, DELETE |
| `/clients/{client_id}/hide` | POST (hide plaintext secret) |
| `/clients/{client_id}/regenerate-secret` | POST |
| `/providers` | GET (list IdP providers) |

✓ Resolved in #993: the canonical `/token` (no trailing slash — the form the
well-known metadata advertises as `token_endpoint`) is now schema-visible, and the
`/token/` trailing-slash alias is `include_in_schema=False`. Routing is unchanged
(FastAPI `redirect_slashes`); OpenAPI now shows a single canonical URL.

---

## DCR_DEFAULT_SCOPE (#608) status

Definition (`backend/src/auth/mcp_scopes.py:56-58`):

```python
DCR_DEFAULT_SCOPES = tuple(s for s in ALL_ADVERTISED_SCOPES if s != "memory:admin")
DCR_DEFAULT_SCOPE  = " ".join(DCR_DEFAULT_SCOPES)
# => "openid memory:read memory:write memory:delete offline_access"
```

Applied in two places: `routes/oauth.py:764` (DCR fallback when client omits
`scope`) and `models/auth.py:530` (`OAuth2Client.scope` column default).
`memory:admin` remains explicitly requestable via the DCR `scope` parameter and
is still advertised in `scopes_supported`.

**Verdict: the current value MATCHES the #608 (D1) narrow intent.** Issue #608
(closed COMPLETED 2026-05-12; PR #615 merged, including the
`e08_592_oauth_scope_canonicalize` backfill migration path) required
narrowing-first: every newly issued DCR token must be admin-less by default
*before* any `require_scope("memory:admin")` route gate lands, so no
already-issued client silently gains admin. That ordering is intact.

Caveats inherited from #608 that remain open at this commit:
- D2/D3 (the `require_scope` dependency + workspace-admin route gates) are not
  yet implemented (tracked for pre-1.0 hardening).
- D4 (`memory:delete` semantics) and D5 (`openid`/`id_token`) remain open as
  designed in #608.

---

## Follow-up candidates

Grouped into 2 proposed sub-issue bundles (issue acceptance allows ≤2).

### Bundle 1 (P1 — scope/metadata honesty; must settle before 1.0 freeze)

- **P1** Scope-enforcement matrix completion: tracked in the internal pre-1.0
  hardening ledger (intentionally out of scope for this public enumeration);
  this doc re-freezes once it lands.
- **P1** `openid` (#608 D5): drop from advertisement (Path B, no deprecation
  needed per #608) or implement `id_token`+`jwks_uri`+`userinfo`; the current
  OIDC discovery doc is non-conformant either way.
- ✅ **DONE (#993)** Advertise `urn:ietf:params:oauth:grant-type:device_code` in
  `grant_types_supported` (both metadata docs) + add `device_authorization_endpoint`
  (RFC 8628 §4). The advertised grant set now matches the live surface.

### Bundle 2 (P2 — token/metadata shape cleanups; nice-to-have before freeze)

- ✅ **DONE (#993)** Deleted the orphaned dashboard JWT module (`auth/jwt.py` — zero
  production callers); no dead claim shape frozen into the 1.0 surface.
- ✅ **DONE (#993)** Removed `resource_signing_alg_values_supported` from
  `oauth-protected-resource` (tokens are opaque; no RS256 key exists).
- ✅ **DONE (#993)** `mcp_sse_endpoint`: retained + declared a stable documented Kagura
  extension; removed `resource_policy_uri` (pointed at Swagger UI; no policy doc exists).
- **P2** Device-flow token-response extras (`user_email`, `workspace_id`,
  `workspace_name`): document stability tier explicitly (display-only,
  non-security-bearing) or namespace them. *(Still open — out of scope for #993.)*
- ✅ **DONE (#993)** `/token` vs `/token/` OpenAPI visibility resolved (canonical
  `/token` visible, `/token/` hidden); `/device/audit-unauth` classified
  documented-internal, excluded from the freeze.
