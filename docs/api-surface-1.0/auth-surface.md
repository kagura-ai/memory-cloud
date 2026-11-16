# Auth / OAuth / DCR Surface — pre-1.0 surface enumeration

> Issue: #622 — pre-1.0 public API surface enumeration and freeze
> Frozen at: `<v1.0.0-rc1 commit — fill in when rc1 is tagged>`
> Source of truth: `backend/tests/api/fixtures/openapi_schema_snapshot.json` (pinned by `backend/tests/api/test_openapi_schema_snapshot.py`). This markdown is human-readable commentary on that fixture: the OAuth / OIDC metadata the schema does not carry, token and credential shapes, and the experimental (satellite-facing) markings; where the two disagree, the fixture wins.
> History: first enumerated at commit 20ae959a2c79cacd2cf7922512ad780f540e9c60 (2026-06-12).
> Changed by #993 (dead JWT path dropped; well-known metadata honesty fixes)

> Re-checked against main 6a940892 (2026-11-16, #622 freeze preparation): the
> MCP OAuth scope catalogue, token shapes and `/.well-known` OAuth metadata
> match what this file already recorded (#993, #1740, #1741, #2773; the field
> counts of the two discovery headings were off by one and are corrected). The
> only new well-known document is `/kmc-sync-configuration`. Everything else
> added since is in
> [Surfaces added since the enumeration](#surfaces-added-since-the-enumeration)
> at the end, with the satellite-facing parts marked **experimental** (#622
> owner decision 3: excluded from the 1.0 semver commitment, may change in a
> MINOR release).

Sources read in full: `backend/src/auth/mcp_scopes.py`,
`backend/src/api/routes/well_known.py`, `backend/src/auth/oauth2_bearer.py`,
`backend/src/auth/dependencies.py` (scope-enforcement sections),
`backend/src/auth/oauth2_server.py` (token-generation/grant sections),
`backend/src/api/routes/oauth.py` (route decorators + DCR/token/introspect handlers),
`backend/src/mcp_server/auth.py`. Enumeration is complete for the items below; no
silent truncation. The 2026-11-16 re-check also read `mcp_server/tools/_scopes.py`,
`auth/oauth_scope.py`, `auth/oidc_server.py`, `auth/oidc_bearer.py`,
`auth/sync_service_jwt.py`, `auth/sync_bearer.py`, `auth/sync_identity.py`,
`auth/share_keys.py`, `auth/oauth_browser.py`, `auth/observation_credentials.py`,
`auth/system_admin_mode.py` and the route files cited in the last section
(module docstrings, route decorators and auth dependencies, not every handler body).

---

## JWT claim shapes

The legacy MCP OAuth surface issues **no JWT tokens**. (The two JWTs that exist
since then belong to other surfaces: the RS256 ID tokens of the experimental
`/oidc` identity provider and the RS256 client assertions sync clients sign; see
the end of this file.) Its credentials (OAuth2 access/refresh
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
Exception (#2773): a `google_connected_apps` client (see `/register` below) keeps
its refresh token while every one of its redirect URIs is still listed in
`GOOGLE_CONNECTED_APPS_REDIRECT_URIS` and it holds no grant beyond
`authorization_code` and `refresh_token`: a refresh answers with the same
`refresh_token` and a new access token that replaces the previous one in the same
row. Removing a URI from the setting or emptying it restores rotation at the
client's next refresh.

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
- Credential prefixes in use at main 6a940892 (each verified where it is minted):

  | Prefix | Credential | Minted in | Tier |
  |---|---|---|---|
  | `kagura_` | API key | `auth/api_keys.py` (`API_KEY_PREFIX`) | frozen |
  | `kagura_sk_` | share key (#1027) | `auth/share_keys.py` (`SHARE_KEY_PREFIX`) | frozen |
  | `kagura_resource_` | resource token | `auth/resource_tokens.py` (`RESOURCE_TOKEN_PREFIX`) | frozen |
  | `kagura_obs_` | observation credential (#2679) | `auth/observation_credentials.py` (`OBSERVATION_KEY_PREFIX`) | experimental |
  | `kmc_oidc_code_` / `kmc_oidc_at_` / `kmc_oidc_rt_` | OIDC provider code / access / refresh token (#2781) | `auth/oidc_server.py` | experimental |
  | `kmc_sync_at_` | sync service token (#2815) | `services/sync_credentials.py` (`TOKEN_PATTERN`) | experimental |
  | (none) | MCP OAuth access / refresh token | `auth/oauth2_server.py` | frozen |

  The `kagura_*` credentials other than API keys never reach the API-key store
  by accident: each is accepted only by its own dependency (share keys by
  `get_share_key_principal`, resource tokens by `auth/resource_tokens.py`,
  observation credentials by `get_observation_principal`). The `kmc_*` tokens
  fall on the OAuth side of `is_oauth_bearer_token`, where they match no row;
  they are accepted only by `auth/oidc_bearer.py` (`kmc_oidc_at_`) and
  `auth/sync_bearer.py` (`kmc_sync_at_`).

---

## well-known metadata endpoints

Router: `backend/src/api/routes/well_known.py`, mounted at prefix `/.well-known`
(`api/main.py`). 6 endpoints total (the sixth, `/kmc-sync-configuration`, is §6
below). The experimental OIDC provider publishes its own discovery document at
`/oidc/.well-known/openid-configuration`, outside this router (see the end of
this file). `base_url` = `FRONTEND_URL` env;
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
| `mcp_endpoint` | `{mcp_base}` (Streamable HTTP) |

✓ `resource_signing_alg_values_supported` and `resource_policy_uri` were **removed
in #993**: access tokens are opaque (no signing alg to advertise — a strict client
would attempt a doomed signature check) and there is no published policy document
(the old value pointed at the Swagger UI `/docs`). Both are OPTIONAL per RFC 9728.
✓ `mcp_endpoint` is a **stable, documented Kagura extension** (non-RFC) naming
the MCP Streamable HTTP endpoint. It replaced `mcp_sse_endpoint` in #1740: that
field advertised `{mcp_base}/sse`, which has answered `410` since the SSE
transport was removed (#248), so no client could use it.

### 3. `GET /.well-known/openid-configuration` (OIDC Discovery) — 16 fields

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
| `revocation_endpoint_auth_methods_supported` | `["none", "client_secret_post", "client_secret_basic"]` (#1741) |
| `introspection_endpoint_auth_methods_supported` | `["client_secret_post", "client_secret_basic"]` (was `["none"]`; #1741, RFC 7662 §2.1) |
| `response_modes_supported` | `["query"]` |
| `subject_types_supported` | `["public"]` |

⚠ Missing OIDC-Discovery-REQUIRED fields `jwks_uri`, `userinfo_endpoint`,
`id_token_signing_alg_values_supported` — and the server never issues an
`id_token`. This is the #608 D5 "OIDC dishonesty", still open at main 6a940892.
The separate `/oidc` issuer (#2781, experimental) publishes `jwks_uri`,
`userinfo_endpoint` and `id_token_signing_alg_values_supported` in its own
document; this one is unchanged.
✓ `grant_types_supported` now advertises `urn:ietf:params:oauth:grant-type:device_code`
and the doc adds `device_authorization_endpoint` (#993, RFC 8628 §4). The device grant
is registered and live, and a `kagura-cli` public client is seeded for it (#624/#627);
the advertised set now matches the real surface.

### 4. `GET /.well-known/oauth-authorization-server` (RFC 8414) — 14 fields

Identical to openid-configuration **minus** `response_modes_supported` and
`subject_types_supported` (the other fields and values match exactly), including the
`device_authorization_endpoint` and device_code grant added in #993.

### 5. `GET /.well-known/google-connected-apps` — 1 field

Domain verification file for Google connected apps (#2773). `200`
`application/json` `{"verification_tokens": [...]}` (the values of
`GOOGLE_CONNECTED_APPS_VERIFICATION_TOKENS`, in the order configured), answered
directly at that path with no redirect; `404` when the setting is empty. Not part
of the OAuth discovery metadata.

### 6. `GET /.well-known/kmc-sync-configuration` — 7 fields

Sync wire-profile document (#2812/#2815), `response_model=SyncConfigurationV1`
(`models/sync_contract.py`, `extra="forbid"`), built by
`services/sync_contract.py:sync_configuration` from settings only;
`Cache-Control: no-store`. Always answered (no 404 when the features are off;
`capabilities` is then empty or partial).

| Field | Value |
|---|---|
| `protocol` | `"kmc.sync.v1"` |
| `schema_version` | `1` |
| `schema_hash` | 64 lowercase hex characters |
| `capabilities` | the enabled subset of `identity.oidc.v1`, `workspace.access.v1`, `sync.installations.v1`, `sync.credentials.v1`, `sync.feeds.v1`, `sync.managed.v1`, `sync.activity.v1`, `public.mcp.v1`, `public.budget.v1`, `public.domains.v1` |
| `human_scopes` | list of OIDC scopes |
| `service_scope` | `"resource.sync"` |
| `assertion_audience` | `"issuer"` |

Experimental, with the rest of the sync surface (owner's decision of
2026-11-16; see the end of this file).

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
hardening ledger and this section is updated with the final matrix before
v1.0.0-rc1. What the code enforces at main 6a940892:

- REST (`auth/dependencies.py`): an OAuth access token needs `memory:read` for
  GET/HEAD/OPTIONS and `memory:write` for every other method (fail closed);
  membership test only, no hierarchy.
- MCP `tools/call` (#1686, `mcp_server/tools/_scopes.py`): `memory:read` for a
  tool whose `readOnlyHint` is true plus `recall` and `get_agent_bootstrap`,
  `memory:write` for every other tool and for unknown names. A token whose
  stored scope names no `memory:*` scope gets its client's registered
  `memory:*` scopes, or the DCR default (`mcp_server/auth.py:granted_scopes`).
  The refusal is HTTP 403 `insufficient_scope`; its challenge `scope` is the
  granted scopes plus the required one. `initialize`, `tools/list`, `ping` and
  `server/discover` are not scope-gated.
- `memory:delete` and `memory:admin` are still required by no route and no
  tool (#608 D3/D4 open); the consent page lists them as "delete" / "manage"
  (`api/routes/oauth.py:_CONSENT_PERMISSION_KEYS`).
- API keys, agent-bound keys and session cookies carry no OAuth scope and are
  not scope-gated.

The experimental OIDC provider has its own, disjoint scope set (`openid`,
`profile`, `workspaces:read`, `integrations:manage`, `sync.records:read`, plus
`resource.sync` for sync service clients); see the end of this file. The two
sets share only the name `openid`; the clients, grants and token stores are
separate, so an `/oidc` token is never accepted on `/mcp` or the memory API and
an MCP OAuth token never on the routes that take an `/oidc` token.

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
| `/register` | POST | DCR (RFC 7591). Public, no auth. Provider whitelist: `chatgpt`/`claude`/`cursor` by redirect-URI hostname; for RFC 8252 loopback redirects (`http://localhost`, `http://127.0.0.1`, `http://[::1]`), a `client_name` naming ChatGPT, Claude, Cursor, Codex, Hermes Agent or OpenClaw (stored as `codex`/`hermes`/`openclaw` for the last three, loopback only, #1657); `gemini` by an exact https redirect on `oauth-redirect.googleusercontent.com`, `oauth-redirect-sandbox.googleusercontent.com` or `oauth-redirect-test.googleusercontent.com` (#2232) (exact host, no suffix match) with the path `/r/user_bound_custom-mcp-` + exactly one segment, never via `client_name` or loopback (#2222); `muse` by the exact callback URIs `https://agent.meta.ai/api/hatch/oauth/callback`, `https://muse.ai/api/hatch/oauth/callback` and `https://platform.muse.ai/oauth/credential-test/callback`, compared as written, never via `client_name` or loopback (#2410, #2487); `grok` by the exact callback URI `https://grok.com/connectors-oauth-exchange-code/` (trailing slash included), compared as written, never via `client_name` or loopback (#2779); `google_connected_apps` by the exact URIs listed in `GOOGLE_CONNECTED_APPS_REDIRECT_URIS` (compared as written, never via `client_name` or loopback), assigned only when every redirect URI the registration keeps is listed there; listed entries sent next to any other qualifying entry are dropped (#2773). Each `redirect_uris` entry is checked on its own; entries that fail are dropped, not stored or echoed, and the registration is refused (`invalid_client_metadata`) only when none passes (#2232). 5 req/min/IP rate limit (addresses in `OAUTH_DCR_RATE_LIMIT_EXEMPT_CIDRS` are exempt, default none, #1741). Always forces `token_endpoint_auth_method="none"`; never returns `client_secret` (#689) |
| `/authorize` | GET | Authorization endpoint (consent page, HTML). The page carries the session-bound `csrf_token` and is served with `Cache-Control: no-store`, `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'` (#2199). Each of `response_type`, `client_id`, `redirect_uri`, `scope`, `state`, `code_challenge`, `code_challenge_method` may be sent once in the query; a repeated one → `400` HTML error page (`invalid_request`), no redirect, nothing stored, checked before the client lookup; `resource` may repeat (RFC 8707), every value must name the MCP resource (#2208) |
| `/authorize` | POST | Consent decision submit. OAuth parameters are read from the query string only; the form carries `confirm`, `default_context` and `csrf_token` (bound to the browser session; any other form field is ignored). Missing or wrong `csrf_token`, on approve and deny alike → `400` HTML error page (`invalid_request`), no redirect, nothing stored; checked after the unregistered-`redirect_uri` page (#218) (#2199). Form body over 16384 bytes → `413` RFC 6749 `{error: "invalid_request", error_description}` + `Cache-Control: no-store`, refused before it is parsed and before the session is read, no redirect (#2200). Each of `response_type`, `client_id`, `redirect_uri`, `scope`, `state`, `code_challenge`, `code_challenge_method` may be sent once in the query; a repeated one → `400` HTML error page (`invalid_request`), no redirect, nothing stored, checked before the unregistered-`redirect_uri` page and the `csrf_token` check (form fields are not counted); `resource` may repeat (RFC 8707), every value must name the MCP resource (#2208) |
| `/token` (canonical, schema-visible), `/token/` (hidden alias) | POST | Token endpoint (Authlib). Grants registered: `authorization_code` (PKCE S256, 10-min single-use codes), `refresh_token` (rotation; a `google_connected_apps` client keeps its refresh token and gets a new access token in place, #2773), `urn:ietf:params:oauth:grant-type:device_code` (RFC 8628, #536). Form body over 16384 bytes → `413 invalid_request` (RFC 6749 shape, `Cache-Control: no-store`), refused before it is parsed (#2200) |
| `/revoke` | POST | RFC 7009 revocation (access or refresh; `token_type_hint` accepted). The caller identifies itself (#1741): a public client by `client_id` (form or HTTP Basic with an empty secret), a confidential client with its secret (`client_secret_basic` or `client_secret_post`); otherwise `401 invalid_client`. A token issued to another client is left alone; it and an unknown token both get `200`. Revoking a refresh token also revokes its access token. For a `google_connected_apps` client that keeps its refresh token, revoking the current access token revokes the refresh token too, also when a refresh of that row runs concurrently; an access token already replaced by a refresh matches no row and gets `200` with nothing changed (#2773). Form body over 16384 bytes → `413 invalid_request` (RFC 6749 shape, `Cache-Control: no-store`), refused before it is parsed and before client authentication; missing or empty `token` → `400 invalid_request`, formerly `422` (#2207) |
| `/introspect` | POST | RFC 7662 introspection. Requires a confidential client's credentials (`client_secret_basic` or `client_secret_post`); an anonymous caller or a public client gets `401 invalid_client` (#1741). Describes only tokens issued to the caller; any other token is `{"active": false}`. Form body over 16384 bytes → `413 invalid_request` (RFC 6749 shape, `Cache-Control: no-store`), refused before it is parsed and before client authentication; missing or empty `token` → `400 invalid_request`, formerly `422` (#2207) |

Device flow (RFC 8628):

| Path | Method | Purpose |
|---|---|---|
| `/device/authorize` | POST | Device authorization request (device_code + user_code). Public, no auth. Body `client_id` + optional `scope` as `application/x-www-form-urlencoded` (RFC 8628 §3.1) or `application/json` (#1671). Per-IP limit `OAUTH_DEVICE_AUTHORIZE_RATE_LIMIT_PER_MINUTE` (default 10/min, #1656), counted before the body is read; over it: `429` RFC 6749 `{error: "invalid_request", error_description}` + `Cache-Control: no-store` + `Retry-After: 60`, no row written. Other errors in the same RFC 6749 shape: unknown `client_id` → `400 invalid_client`; missing `client_id`, malformed body, missing or other `Content-Type` → `400 invalid_request`; body over 4096 bytes → `413 invalid_request`; storage failure → `500 server_error` |
| `/device/verify` | POST | Verify a user_code (browser side). Public, no auth. Per-IP limit `OAUTH_DEVICE_VERIFY_RATE_LIMIT_PER_MINUTE` (default 30/min, #1656), counted before the lookup whether the code exists or not; over it: `429` canonical `{error, message, details}` envelope + `Retry-After: 60` |
| `/device/confirm` | POST | User confirms/denies the device grant |
| `/device/audit-unauth` | POST | Internal frontend audit beacon (#779); `include_in_schema=False`. Classified **documented-internal** (#993) — intentionally excluded from the public 1.0 surface |

The two device-flow limits count per `request.client.host` over a 60 s window
in Redis and let requests through when Redis is unavailable. Behind a reverse
proxy the key is the caller's address only when uvicorn trusts the proxy's
forwarded headers (`--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`). Expired
`oauth_device_codes` rows are deleted hourly once they are
`OAUTH_DEVICE_CODE_RETENTION_SECONDS` (default 3600) past expiry.

Client-management (session-authenticated dashboard surface, not part of the
RFC-discovery contract but same router/prefix):

| Path | Methods |
|---|---|
| `/clients` | GET (list), POST (create, returns secret once) |
| `/clients/{client_id}` | GET, PUT, DELETE |
| `/clients/{client_id}/hide` | POST (hide plaintext secret) |
| `/clients/{client_id}/regenerate-secret` | POST |
| `/providers` | GET (list IdP providers) — behind `require_admin`, so it follows `SYSTEM_ADMIN_MODE` (#2767) and is experimental with the other admin routes (see `rest-response-models.md`) |

✓ Resolved in #993: the canonical `/token` (no trailing slash — the form the
well-known metadata advertises as `token_endpoint`) is now schema-visible, and the
`/token/` trailing-slash alias is `include_in_schema=False`. Routing is unchanged
(FastAPI `redirect_slashes`); OpenAPI now shows a single canonical URL.

---

## DCR_DEFAULT_SCOPE (#608) status

Definition (`backend/src/auth/mcp_scopes.py`):

```python
DCR_DEFAULT_SCOPES = tuple(s for s in ALL_ADVERTISED_SCOPES if s != "memory:admin")
DCR_DEFAULT_SCOPE  = " ".join(DCR_DEFAULT_SCOPES)
# => "openid memory:read memory:write memory:delete offline_access"
```

Applied in three places: `auth/oauth_scope.py:registration_scope` (DCR: a
request without `scope`, or whose defined scopes include no `memory:*` scope,
is registered with the default, keeping any `openid` / `offline_access` it asked
for; #1686), the `scope` default of the dashboard client-create request model in
`api/routes/oauth.py`, and `models/auth.py` (`OAuth2Client.scope` column default).
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

## Surfaces added since the enumeration

Re-checked against main 6a940892 (2026-11-16). Routes are under `/api/v1`
unless noted; their request / response models are in the OpenAPI snapshot,
which stays the source of truth. Two tiers: **frozen** (part of the 1.0
commitment like the sections above) and **experimental** (#622 decision 3 and
the owner's decisions of 2026-11-16: excluded from the 1.0 commitment, may
change in a MINOR release).

### Browser sign-in and account credentials — frozen

Session-cookie surfaces of the web UI (cookie `kagura_session`,
`auth/session.py`; HttpOnly).

- **Email + password sign-up** (#1734) — `api/routes/email_signup.py`,
  `/auth/signup/email/request` (always `202`, same body), `/preview`,
  `/complete` (sets the session cookie, answers `{"redirect_url"}`). The emailed
  link is the credential. `ENABLE_EMAIL_SIGNUP=false` → every route `404`.
- **Password login, reset and settings** (#1678) — `api/routes/auth.py`
  `POST /auth/login` (`PasswordLoginRequest {login_id, password, accepted_terms?}`,
  `login_id` = login id or verified email; `PasswordLoginResponse {success,
  mfa_required, mfa_session_token?, redirect_url?}`); `api/routes/password.py`
  `/auth/password/reset-request` (`202`), `/auth/password/reset`,
  `/auth/password/setup`, and session-only `/me/password/setup-request`,
  `/me/password/change`, `DELETE /me/password`.
- **TOTP MFA** — `api/routes/mfa_settings.py` (session only):
  `/auth/mfa/setup`, `/auth/mfa/enable`, `/auth/mfa/recovery-codes/regenerate`,
  `/auth/mfa/disable`. Sign-in second step `POST /auth/mfa/verify`
  (`api/routes/auth.py`, `MfaVerifyRequest {mfa_session_token, totp_code}`;
  `totp_code` also takes a recovery code, `auth/recovery_codes.py`). The TOTP
  secret is stored encrypted (`auth/totp.py`, `utils/encryption.py`).
- **Passkeys** (#2645, discoverable, user-verified WebAuthn) —
  `api/routes/passkeys.py`, `/auth/passkeys`: `GET ""`, `POST /proof/options`,
  `POST /register/options`, `POST /register/verify`,
  `POST /authenticate/options`, `POST /authenticate/verify`,
  `PATCH /{key_id}`, `DELETE /{key_id}`.
- **Provider link confirmation** (#2786) — `api/routes/provider_link_confirm.py`,
  `/auth/link-confirm/preview` (consumes nothing, reports `same_browser`),
  `/complete` (session cookie + `{"redirect_url"}`; `403 AUTH-310` in another
  browser, `400 AUTH-301` unknown / expired / used link), `/cancel` (always
  `204`). `ENABLE_PROVIDER_LINK_CONFIRMATION=false` → every route `404`.
  The "established link" rule is `auth/provider_link.py`.
- **OAuth browser nonce cookie** (#2786, #2791) — `auth/oauth_browser.py`.
  `GET /auth/{google,github}/login` sets an HttpOnly nonce cookie,
  `__Host-kagura_oauth_browser` when cookies are `Secure` (plain
  `kagura_oauth_browser` over http), lifetime 20 minutes, and binds its digest
  to the OAuth `state`. A callback opened in a browser that did not start the
  sign-in creates nothing and redirects to `/login?error=oauth_other_browser`
  (`api/routes/auth.py`, `_OAUTH_ERROR_REASONS` = `oauth_failed`,
  `oauth_expired`, `provider_unlinked`, `oauth_other_browser`).
- **Connected OAuth grants** — `api/routes/me_oauth_grants.py`
  (`/me/oauth/grants`: `GET`, `PUT`, `DELETE`) and `api/routes/me_oauth.py`
  (`POST /me/refresh-oauth`).

### OAuth device code grant — frozen, unchanged

Re-checked: the device routes, rate limits and token-response extras
(`user_email`, `workspace_id`, `workspace_name`; `auth/oauth2_server.py`
`DeviceAuthorizationGrant`) are as described above; tokens live 3600 s like the
other two grants.

### Share keys — frozen

`auth/share_keys.py`, `kagura_sk_` + `token_urlsafe(32)`, SHA-256 hash stored,
bound to one context, scope `memory:read` only, always expiring (default and
maximum 30 days). Managed at `/config/share-keys` (`POST`, `GET`,
`POST /{key_id}/revoke`, `key_id` = `skey_…`); accepted only by
`get_share_key_principal` (`auth/dependencies.py`) on `POST /share/recall` and
`GET /share/sessions` (`api/routes/share_keys.py`). `GET /share/sessions` is
experimental (see "Other dashboard-facing routes" below); the key itself and
`POST /share/recall` are frozen.

### Resource tokens — frozen, unchanged

`kagura_resource_` + `token_urlsafe(32)` (`auth/resource_tokens.py`), public id
`rtok_…` (#1008), capability-typed, resource + workspace scoped.

### Observation credentials — experimental (#2678, #2679)

`auth/observation_credentials.py`: `kagura_obs_` + `token_urlsafe(32)`, public
id `obsk_…`, SHA-256 hash stored, lifetime 1–365 days (default 90), rotation
keeps the binding. Accepted only by `get_observation_principal` on
`/observability/*`, and only when `OBSERVABILITY_API_ENABLED` is on and the
caller is inside `OBSERVABILITY_API_ALLOWED_CIDRS` (else `404`); per-credential
rate limit. Minted by a workspace owner at `/observation-credentials`. Routes
and models: `rest-response-models.md` (experimental section).

### System-admin service credential — experimental (#2767, #2851)

With `SYSTEM_ADMIN_MODE=service` the admin routes take `Authorization: Bearer
<SYSTEM_ADMIN_SERVICE_TOKEN>` (or a `SYSTEM_ADMIN_SERVICE_TOKEN_PREVIOUS`
value) from `SYSTEM_ADMIN_SERVICE_ALLOWED_CIDRS` plus one `X-Operator-Email`
header, and serve an `operator:<address>` principal
(`auth/system_admin_mode.py`, `auth/dependencies.py:require_admin`); errors
`SYSADMIN-001`..`004`. The session cookie is ignored in that mode. Routes:
`rest-response-models.md` (experimental section).

### Identity-only OIDC provider — experimental (#2781, #2793, #2794)

Mounted at `/oidc` (`api/routes/oidc.py`, not under `/api/v1`), `404` on every
route unless `OIDC_ENABLED`. Clients are confidential web clients provisioned by
an operator (`cli/oidc_client.py`), never by DCR; clients, grants and tokens
live in their own tables (`models/oidc.py`), separate from MCP OAuth.

- **Routes**: `GET /oidc/.well-known/openid-configuration`, `GET /oidc/jwks`
  (public RSA parameters only), `GET|POST /oidc/authorize`, `POST /oidc/token`,
  `GET /oidc/userinfo`, `POST /oidc/revoke`.
- **Discovery fields**: `issuer`, `authorization_endpoint`, `token_endpoint`,
  `jwks_uri`, `userinfo_endpoint`, `revocation_endpoint`,
  `response_types_supported` `["code"]`, `response_modes_supported`
  `["query"]`, `grant_types_supported` `["authorization_code",
  "refresh_token"]` (+ `client_credentials` when sync credentials are on),
  `subject_types_supported` `["public"]`, `id_token_signing_alg_values_supported`
  `["RS256"]`, `token_endpoint_auth_methods_supported` `["client_secret_basic",
  "client_secret_post"]` (+ `private_key_jwt` and
  `token_endpoint_auth_signing_alg_values_supported` `["RS256"]` when sync
  credentials are on), `revocation_endpoint_auth_methods_supported`,
  `code_challenge_methods_supported` `["S256"]`, `scopes_supported`,
  `claims_supported`.
- **Scopes** (`auth/oidc_server.py:IDENTITY_SCOPES`): `openid` (required),
  `profile`, `workspaces:read`, `integrations:manage`, `sync.records:read`;
  `resource.sync` is the sync service scope. A request must stay within the
  client's registered scope; PKCE S256 and a nonce (≤255 chars) are mandatory.
- **Tokens**: code `kmc_oidc_code_…` (one use, client bound), access token
  `kmc_oidc_at_…` (`expires_in` = min(300, the grant's remaining life)),
  refresh token `kmc_oidc_rt_…`; only SHA-256 hashes are stored.
- **ID token** (RS256, header `kid` = active key): `iss`, `sub` (the user's
  `user_id`), `aud` (`[client_id]`), `iat`, `exp` (`iat` + 300), `nonce`,
  `auth_time`, `at_hash`, and with `profile` `name` / `picture` when set.
  UserInfo (`kmc_oidc_at_` bearer only) answers `sub` plus the same profile
  claims (`auth/oidc_server.py:profile`).
- **Bearer routes** (`auth/oidc_bearer.py`, `kmc_oidc_at_` only, scope
  `workspaces:read`): `GET /me/workspaces` and
  `GET /me/workspaces/{workspace_id}/access` (`api/routes/me_workspaces.py`,
  #2794).

### Sync service credentials — experimental (owner's decision of 2026-11-16)

Built on the OIDC provider and off unless `OIDC_ENABLED`,
`SYNC_INTEGRATIONS_ENABLED` and `SYNC_CREDENTIALS_ENABLED` are all on.

- **Client assertion** (`auth/sync_service_jwt.py`, #2806): RFC 7523
  `private_key_jwt` at `POST /oidc/token` (`grant_type=client_credentials`,
  `scope=resource.sync`, `client_assertion_type` =
  `urn:ietf:params:oauth:client-assertion-type:jwt-bearer`). Header `alg` RS256,
  `kid` 1–128 of `[A-Za-z0-9._-]`, `typ` absent / `JWT` /
  `client-authentication+jwt`. Claims: exactly `iss` = `sub` = `client_id`,
  `aud` = the issuer, integer `iat` / `exp` with `exp - iat` ≤ 60 s, optional
  `nbf`, `jti` 16–128 of `[A-Za-z0-9._-]` (single use, stored as a hash).
  Signing keys are locally registered RSA 2048–4096 public JWKs (no remote JWKS).
- **Service token** (`services/sync_credentials.py`, #2815): `kmc_sync_at_` +
  43 characters, `expires_in` 300, scope `resource.sync`, installation bound;
  accepted only by `auth/sync_bearer.py:sync_service_token` on
  `/sync/installations/{installation_id}` (`api/routes/sync_worker.py`) and the
  run routes under `/sync/installations/{installation_id}/…`
  (`api/routes/sync_feeds.py`).
- **Human control routes** take an OIDC access token with
  `integrations:manage`, `workspaces:read` or `sync.records:read`
  (`auth/sync_identity.py`): `/workspaces/{workspace_id}/sync-authorizations`,
  `/workspaces/{workspace_id}/sync-installations[/…]` (installations, records,
  management, activity; `api/routes/sync_installations.py`,
  `sync_records.py`, `sync_management.py`, `sync_activity.py` — the scope per
  route in the last two is unverified).
- Discovery: `/.well-known/kmc-sync-configuration` (§6 of the well-known section).

The whole sync surface is experimental: the client assertion, the
`kmc_sync_at_` token, every `/sync/*` and `/workspaces/*/sync-*` route (full
list with files in `rest-response-models.md`, section 6) and
`/.well-known/kmc-sync-configuration`. #2865 (open PR #2867: feed-config reads
and PUT preconditions) adds to this surface and inherits its experimental
status.

### Other dashboard-facing routes — experimental (owner's decision of 2026-11-16)

- `GET /share/sessions` (`api/routes/share_keys.py`, share-key auth): its
  docstring (#1064) names "an external, observe-only dashboard" rendering live
  agent status.
- `GET /resources/{resource_id}/public-mcp/usage-health`
  (`api/routes/public_mcp_usage.py`, `WorkspaceOwner`, #2678): owner-facing
  health of the public MCP usage events the observability API exports.

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
  Superseded in #1740: `mcp_sse_endpoint` (a `410` endpoint since #248) was replaced
  by `mcp_endpoint`, the Streamable HTTP endpoint.
- **P2** Device-flow token-response extras (`user_email`, `workspace_id`,
  `workspace_name`): document stability tier explicitly (display-only,
  non-security-bearing) or namespace them. *(Still open — out of scope for #993.)*
- ✅ **DONE (#993)** `/token` vs `/token/` OpenAPI visibility resolved (canonical
  `/token` visible, `/token/` hidden); `/device/audit-unauth` classified
  documented-internal, excluded from the freeze.
