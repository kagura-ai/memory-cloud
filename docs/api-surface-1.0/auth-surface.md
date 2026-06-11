# Auth / OAuth / DCR Surface — pre-1.0 surface enumeration

> Issue: #622 — pre-1.0 public API surface enumeration and freeze
> Enumerated at commit 20ae959a2c79cacd2cf7922512ad780f540e9c60 (main HEAD, 2026-06-12)

Sources read in full: `backend/src/auth/jwt.py`, `backend/src/auth/mcp_scopes.py`,
`backend/src/api/routes/well_known.py`, `backend/src/auth/oauth2_bearer.py`,
`backend/src/auth/dependencies.py` (scope-enforcement sections),
`backend/src/auth/oauth2_server.py` (token-generation/grant sections),
`backend/src/api/routes/oauth.py` (route decorators + DCR/token/introspect handlers),
`backend/src/mcp_server/auth.py`. Enumeration is complete for the items below; no
silent truncation.

---

## JWT claim shapes

The platform issues exactly **one JWT token type**. All other credentials (OAuth2
access/refresh tokens, API keys, resource tokens, browser sessions) are **opaque
random strings, not JWTs** — they carry no claims; their attributes live in DB rows.

### 1. Dashboard JWT access token — `auth/jwt.py:create_access_token`

Signed `HS256` (default, `settings.jwt_algorithm`) with shared secret
`settings.jwt_secret`; lifetime `settings.jwt_expire_minutes` (default 60).
Algorithm is enforced on verify (mismatch → reject). 5 claims, all always present:

| Claim | Type | Meaning | Presence |
|---|---|---|---|
| `sub` | string | User identifier (IdP-qualified, e.g. `google\|123`) | always |
| `email` | string | User email | always |
| `role` | string | App role: `admin` / `user` / `read_only` (custom claim) | always |
| `iat` | NumericDate | Issued-at | always |
| `exp` | NumericDate | Expiry (`iat` + `jwt_expire_minutes`) | always |

No `iss`, `aud`, `jti`, `nbf`, no workspace/tenant claim, no `token_type`
discriminator, no scope claim.

⚠ **`create_access_token` / `verify_access_token` have zero production callers**
(grep across `backend/src` excluding tests finds no import of `auth.jwt`). The
live dashboard auth is an opaque session cookie (`auth/session.py`,
`secrets.token_urlsafe(32)` session id resolved by SessionMiddleware). Freezing
this JWT shape at 1.0 would freeze dead surface — decide remove-or-adopt before
the freeze.

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

### 2. `GET /.well-known/oauth-protected-resource` (RFC 9728) — 8 fields

| Field | Value |
|---|---|
| `resource` | `{mcp_base}` (MCP canonical URL) |
| `authorization_servers` | `[{base_url}]` |
| `scopes_supported` | `ALL_ADVERTISED_SCOPES` (6 scopes, see catalogue) |
| `bearer_methods_supported` | `["header"]` |
| `resource_signing_alg_values_supported` | `["RS256", "HS256"]` |
| `resource_documentation` | `{base_url}/redoc` |
| `resource_policy_uri` | `{base_url}/docs` |
| `mcp_sse_endpoint` | `{mcp_base}/sse` |

⚠ `resource_signing_alg_values_supported` advertises RS256/HS256 but access tokens
are opaque (never signed, and no RS256 key exists anywhere). Misleading for a
frozen surface.
⚠ `mcp_sse_endpoint` is a custom, non-RFC field — declare as a stable extension
or rename before freeze.
⚠ `resource_policy_uri` points at the Swagger UI (`/docs`), not a policy document.

### 3. `GET /.well-known/openid-configuration` (OIDC Discovery) — 14 fields

| Field | Value |
|---|---|
| `issuer` | `{base_url}` |
| `authorization_endpoint` | `{base_url}/api/v1/oauth/authorize` |
| `token_endpoint` | `{base_url}/api/v1/oauth/token` |
| `revocation_endpoint` | `{base_url}/api/v1/oauth/revoke` |
| `introspection_endpoint` | `{base_url}/api/v1/oauth/introspect` |
| `registration_endpoint` | `{base_url}/api/v1/oauth/register` |
| `code_challenge_methods_supported` | `["S256"]` |
| `scopes_supported` | `ALL_ADVERTISED_SCOPES` (6) |
| `response_types_supported` | `["code"]` |
| `grant_types_supported` | `["authorization_code", "refresh_token"]` |
| `token_endpoint_auth_methods_supported` | `["none", "client_secret_post", "client_secret_basic"]` |
| `introspection_endpoint_auth_methods_supported` | `["none"]` |
| `response_modes_supported` | `["query"]` |
| `subject_types_supported` | `["public"]` |

⚠ Missing OIDC-Discovery-REQUIRED fields `jwks_uri`, `userinfo_endpoint`,
`id_token_signing_alg_values_supported` — and the server never issues an
`id_token`. This is the #608 D5 "OIDC dishonesty", still open at this commit.
⚠ `grant_types_supported` omits `urn:ietf:params:oauth:grant-type:device_code`
although the device grant is registered and live (see endpoints section) and a
`kagura-cli` public client is seeded for it (#624/#627). Advertised set is
narrower than the real surface.

### 4. `GET /.well-known/oauth-authorization-server` (RFC 8414) — 12 fields

Identical to openid-configuration **minus** `response_modes_supported` and
`subject_types_supported` (the other 12 fields and values match exactly).
Same device_code omission applies.

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
| `/token`, `/token/` | POST | Token endpoint (Authlib). Grants registered: `authorization_code` (PKCE S256, 10-min single-use codes), `refresh_token` (rotation), `urn:ietf:params:oauth:grant-type:device_code` (RFC 8628, #536) |
| `/revoke` | POST | RFC 7009 revocation (access or refresh; `token_type_hint` accepted) |
| `/introspect` | POST | RFC 7662 introspection; auth method `none` (public) |

Device flow (RFC 8628):

| Path | Method | Purpose |
|---|---|---|
| `/device/authorize` | POST | Device authorization request (device_code + user_code) |
| `/device/verify` | POST | Verify a user_code (browser side) |
| `/device/confirm` | POST | User confirms/denies the device grant |
| `/device/audit-unauth` | POST | Internal frontend audit beacon (#779); `include_in_schema=False` — ⚠ should be declared non-public surface |

Client-management (session-authenticated dashboard surface, not part of the
RFC-discovery contract but same router/prefix):

| Path | Methods |
|---|---|
| `/clients` | GET (list), POST (create, returns secret once) |
| `/clients/{client_id}` | GET, PUT, DELETE |
| `/clients/{client_id}/hide` | POST (hide plaintext secret) |
| `/clients/{client_id}/regenerate-secret` | POST |
| `/providers` | GET (list IdP providers) |

⚠ `/token` is `include_in_schema=False` on the canonical path while `/token/`
(trailing slash) is schema-visible — cosmetic OpenAPI inconsistency on the single
most important OAuth URL.

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
- **P1** Advertise `urn:ietf:params:oauth:grant-type:device_code` in
  `grant_types_supported` (both metadata docs) — it is live, seeded for
  kagura-cli, and a frozen metadata doc that under-advertises grants is a
  compat trap.

### Bundle 2 (P2 — token/metadata shape cleanups; nice-to-have before freeze)

- **P2** Delete or adopt the orphaned dashboard JWT module (`auth/jwt.py` — zero
  production callers); do not freeze dead claim shapes into the 1.0 surface.
- **P2** Remove `resource_signing_alg_values_supported` from
  `oauth-protected-resource` (tokens are opaque; no RS256 key exists).
- **P2** `mcp_sse_endpoint`: declare as a stable documented extension or move
  behind a namespaced key; fix `resource_policy_uri` pointing at Swagger UI.
- **P2** Device-flow token-response extras (`user_email`, `workspace_id`,
  `workspace_name`): document stability tier explicitly (display-only,
  non-security-bearing) or namespace them.
- **P2** `/token` vs `/token/` OpenAPI visibility inconsistency; decide whether
  `/device/audit-unauth` is public surface (recommend: documented-internal,
  excluded from the freeze).
