# API Reference

Kagura Memory Cloud provides both REST APIs and 72 MCP tools (Model Context Protocol) for AI memory management.

## Overview

- **REST API Base URL**: `http://localhost:8080/api/v1`
- **MCP Server Endpoint**: `http://localhost:8080/mcp` (Streamable HTTP transport)
- **OpenAPI Specification**: `http://localhost:8080/openapi.json`

## Authentication

### Password sign-in with two-factor authentication

`GET /api/v1/auth/me` includes `user.mfa_enabled` and `user.mfa_recovery_codes_remaining`. An account with a password can manage its authenticator with a signed-in browser session.

| Endpoint | Request | Result |
|---|---|---|
| `POST /api/v1/auth/mfa/setup` | `current_password` | A base32 `secret` and `provisioning_uri` to display locally. Pending setup expires after five minutes and is bound to this session and credential state. |
| `POST /api/v1/auth/mfa/enable` | `code` from the new authenticator | Enables TOTP and returns eight single-use `recovery_codes`. Save them before closing the dialog. |
| `POST /api/v1/auth/mfa/recovery-codes/regenerate` | `current_password` and a fresh authenticator or unused recovery `code` | Replaces all old codes, signs out other browser sessions and returns eight new codes once. Also available to existing CLI admins with zero codes. |
| `POST /api/v1/auth/mfa/disable` | `current_password`, plus an authenticator or recovery `code` | HTTP 204. Invalidates the secret and every remaining recovery code. |

Enabling or disabling MFA signs out the account's other browser sessions and sends the owner a security notification. A password login that returns `mfa_required` finishes at `POST /api/v1/auth/mfa/verify`; its existing `totp_code` field accepts either the authenticator value or a recovery code. Failed attempts share the account's password sign-in budget. OAuth sign-in continues to use the provider's authentication. A password reset retains MFA, removes the account's passkeys (#2660) and disconnects its Google / GitHub sign-ins (#2676), so an account with MFA still needs its authenticator or a recovery code afterwards. Disable MFA before removing the password. For lockouts, an operator can run `python -m src.cli.reset_password` with the login ID or user ID and choose “Disable MFA only”.

Successful recovery-code sign-ins consume one code, append a `recovery_code_used` audit entry and notify the account owner. A failed sign-in releases its reservation. If a worker stops mid-sign-in, the reservation expires after five minutes. Code regeneration also writes an audit entry and notifies the owner. Creating an admin with MFA or re-enabling MFA in the operator CLI displays a new recovery set after the change commits.

All API requests require authentication using one of the following methods:

### 1. API Key (Recommended for programmatic access)

```bash
curl -H "Authorization: Bearer kagura_xxxxxxxxxxxx" \
  http://localhost:8080/api/v1/memory/recall
```

API keys come in three scoping shapes:

| Scope | Created via | Access | Issue |
|---|---|---|---|
| Owner-scoped | `POST /api/v1/config/api-keys` | All of the owner's contexts (current workspace) | — |
| Workspace-scoped | `POST /api/v1/workspaces/{wsid}/members/{uid}/credentials/api-keys` | All contexts in one workspace | #169 |
| Public-bound | Same as workspace-scoped, with `bound_context_id` in the body | One `is_public=true` context only — for attributed access to `/api/v1/public/{ctx}/*` (per-key rate limit, audit, independent revocation) | #626 |

Public-bound keys are immutable: to change which context a key attributes to, revoke the key and create a new one. They cannot also be workspace-scoped (DB CHECK constraint).

A workspace-scoped key you mint for yourself (session only) takes an optional `default_context_id` ([#2188](https://github.com/kagura-ai/memory-cloud/issues/2188)), the key's MCP default context with the same rules as the [config route](#patch-apiv1configapi-keyskey_id): a context you cannot read within the key's workspace is `404` `Context not found` and no key is created, and a public-bound key cannot carry one (`422`). An owner-provisioned key for another member cannot carry one either (`400`). The member key list and create responses carry `default_context_id`, `default_context_name` (`null` when none is set or when you, the viewer, cannot read the context) and `agent_id` (`null` unless the key is agent-bound). A default is changed later with `PATCH /api/v1/config/api-keys/{key_id}`.

### 2. OAuth2 Access Token

```bash
curl -H "Authorization: Bearer <access_token>" \
  http://localhost:8080/api/v1/memory/recall
```

### 3. Session Cookie (Web UI)

Session-based authentication for the web management interface.

The OAuth sign-in endpoints (`GET /api/v1/auth/{google,github}/login`) also take `locale` (`en` or `ja`, the sign-in page's language) and `timezone` (an IANA name such as `Asia/Tokyo`, the browser's time zone). Both are bound to the OAuth state like `accepted_terms`, and an account created by that sign-in starts with them instead of `en` / `UTC`. An existing account keeps its own settings, and an unsupported value is ignored. With `ANNOUNCEMENT_EMAILS_ENABLED` on they also take `announcement_emails` (`true` / `false`) and `announcement_consent_version`, the answer to the sign-up opt-in for announcement emails (Issue #2761) — see [Announcement-email consent](#announcement-email-consent).

Both modes of these endpoints (a `303` to the provider with `return_to`, JSON `{authorization_url, state}` without) set an HttpOnly sign-in cookie, and the sign-in finishes only in the browser that holds it: the provider callback answers `/login?error=oauth_other_browser&provider=<p>` otherwise and creates nothing. A client of the JSON mode must open `authorization_url` with the cookie jar that received the response's `Set-Cookie`. The `redirect_uri` parameter of the Google endpoint is ignored; the configured `GOOGLE_REDIRECT_URI` is always used. See [deployment](deployment.md#a-google-or-github-sign-in-finishes-in-the-browser-that-started-it-issue-2791).

---

### Passkeys

The prefix for these endpoints is `/api/v1/auth/passkeys`. Management uses a browser
session. A maximum of ten passkeys may be registered per account. Public key
credential IDs and WebAuthn binary fields travel in base64url form.

| Method and path | Request and result |
|---|---|
| `POST /register/options` | Submit a current password, a fresh passkey proof, or an empty object for recent session proof. Returns discoverable credential creation options. |
| `POST /register/verify` | Submit the options' `challenge` and the browser's `credential`. Returns 201 with the registered key's ID, label, and timestamps. |
| `POST /authenticate/options` | Anonymous. Returns assertion options and sets the browser ceremony cookie. |
| `POST /authenticate/verify` | Anonymous. Submit `challenge`, `credential`, and optional `accepted_terms`, with cookies included. Success sets the session cookie and returns `redirect_url`. The optional `return_to` query uses the ordinary sign-in redirect rules. Five assertions per account per five minutes, counted separately from the password sign-in budget; a completed sign-in clears the account's password sign-in failures as well. |
| `GET /` (prefix without trailing slash) | Lists the active account's IDs, labels and timestamps. `?include_limit=true` returns `{keys: [...], limit: N}` so clients can disable registration at the server's limit; the default remains an array. |
| `PATCH /{key_id}` | Supply `name` (1–100 characters). |
| `POST /proof/options` | Obtain assertion options for another registration, or use `?removing={key_id}` to prove a different key for removal. `?linking={provider}` proves a key for connecting a sign-in provider (`POST /api/v1/me/account/link-provider`). Submit the assertion as `assertion` alongside `challenge` on the management request. |
| `DELETE /{key_id}` | Submit `current_password` or the scoped assertion proof. Returns 204. Removing the last available sign-in method returns 409. |

An account with a linked Google or GitHub provider, one passkey and no password gets
403 on removal until it can prove a different credential. From Profile → Passkeys,
follow **Set up a password by email**, request the link in the Password section, and
finish setup from that mailbox. Return to Passkeys and enter the new password to
remove the key. Alternatively, register another passkey and confirm removal with it.

A passkey assertion requires local user verification and signs in an existing account
without a subsequent TOTP prompt. Account deletion cascades to all its credentials.

Registration checks the account limit before password or passkey proof and again
when persisting the credential. A limit refusal returns 409 with
`details.reason="passkey_limit"` and `details.limit`. The two password setup
buttons on Profile share their pending/sent state; a replacement link can be
requested explicitly if the first link is lost.

### Workspace invitation availability

Pending invitations and token previews include `plan_blocked` when the workspace's
current plan prevents acceptance. The invitation stays visible, with guidance to
contact its owner. Such an invitation does not admit a new account while registration
is closed, and does not suppress creation of a new user's personal workspace.
Acceptance still checks the workspace's current plan at the time of the request.

Addressed invitations need a verified address: `GET /api/v1/invitations/pending`
lists, and `POST /api/v1/invitations/accept` accepts, an invitation addressed to an
email address only when the signed-in account's address is verified; otherwise the
list is empty and accept answers `400`. A link-only invitation is accepted as before.

The public preview `GET /api/v1/invitations/{token}` also returns
`email_signup_available`: whether a person without an account can create one with
this invitation's address by email. True when email sign-up is on and the sign-up
controls admit it: the invitation is addressed to an email, is not an owner-role
invitation, and its workspace exists on a plan that allows invitations (signup gate
on or off, see
[deployment](deployment.md#workspace-invitations-and-the-signup-gate-issue-2785));
or the gate is off and `ALLOW_REGISTRATION=true`; or no account exists yet. It
names no address.

### Email + password sign-up

Public, `404` unless `ENABLE_EMAIL_SIGNUP=true` (see
[deployment](deployment.md#email-sign-up-issue-1734)). The emailed link is the
credential.

| Method / path | Body | Answers |
|---|---|---|
| `POST /api/v1/auth/signup/email/request` | `{"email", "invite"?, "locale"?, "timezone"?}` | `202 {"status": "accepted", "message": ...}` for every address; `429` per client address; `422` only for an address of the wrong shape (one ASCII mailbox) |
| `POST /api/v1/auth/signup/email/preview` | `{"token"}` | `200 {"email", "expires_at"}`; `400 AUTH-301` unknown, expired or used link |
| `POST /api/v1/auth/signup/email/complete` | `{"token", "new_password", "name"?, "accepted_terms"?, "return_to"?, "announcement_emails"?, "announcement_consent_version"?}` | `200 {"redirect_url"}` plus the session cookie; `400 AUTH-301`; `403 AUTH-308` the sign-up controls refuse it now; `409 AUTH-309` the address has an account; `422` password policy (`field: new_password`) or terms (`field: accepted_terms`); `429`; `503` the account was created but no session could be opened (sign in with the password) |

`invite` is a beta invite token (a malformed one is ignored). The request body
has no return path: the page that started the sign-up keeps it and sends it as
`return_to` on completion, validated like the password sign-in's (same origin
only). The policy and terms are checked before the link is spent.
`announcement_emails` is the answer to the announcement-email opt-in shown on
the completion page, sent with the `announcement_consent_version` from
`/system/info`; the new account records it (an absent answer, a stale version or
the feature switched off records nothing). The request body never carries it.

### Provider link confirmation

Public, `404` when `ENABLE_PROVIDER_LINK_CONFIRMATION=false` (see
[deployment](deployment.md#connecting-github-to-an-email-account-by-email-issue-2786)).
The emailed token and the browser nonce cookie set by
`GET /api/v1/auth/{google,github}/login` (the same cookie every Google / GitHub
sign-in needs to finish) are the credentials; send requests with cookies
(`credentials: "include"`).

| Method / path | Body | Answers |
|---|---|---|
| `POST /api/v1/auth/link-confirm/preview` | `{"token"}` | `200 {"provider", "provider_login", "provider_name", "provider_avatar_url", "expires_at", "same_browser"}`; `400 AUTH-301` unknown, expired or used link; `429` |
| `POST /api/v1/auth/link-confirm/complete` | `{"token"}` | `200 {"redirect_url"}` plus the session cookie; `403 AUTH-310` not the browser that started the sign-in (the link stays usable there); `400 AUTH-301` unknown, expired or used link, or an account that cannot take the link any more; `429`; `503` the provider was connected but no session could be opened (sign in again) |
| `POST /api/v1/auth/link-confirm/cancel` | `{"token"}` | `204` always ("This wasn't me": the link is discarded and the account gets no further offers and no automatic Google link); `429` |

Preview consumes nothing. `provider` is `github` or `google`; `provider_login`
is the GitHub login or the Google address (show it as the primary
identifier); `provider_name` is free text from the provider (at most 80
characters, secondary); `provider_avatar_url` is `null` or an https URL on the
provider's avatar host. Only `same_browser: true` can complete.

## Memory APIs

### POST /api/v1/memory/remember

Store a new memory with 3-layer architecture (summary, context_summary, details).

**Request Body:**

```json
{
  "summary": "User prefers dark mode in development tools",
  "content": "The user explicitly stated they prefer dark color schemes...",
  "type": "preference",
  "tags": ["ui", "preferences"],
  "context_summary": "Conversation about IDE settings and developer workflow",
  "details": {
    "ide": "VSCode",
    "theme": "Monokai Pro",
    "font_size": 14
  },
  "importance": 0.8
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `summary` | string | Yes | Concise summary (10-500 chars) for search |
| `content` | string | Yes | Main content of the memory |
| `type` | string | Yes | Memory type: `code`, `note`, `decision`, `bug-fix`, etc. |
| `tags` | array[string] | No | Tags for filtering (e.g., `["python", "auth"]`) |
| `context_summary` | string | No | Contextual explanation (max 2000 chars) |
| `details` | object | No | Structured metadata (JSON) |
| `importance` | float | No | Importance score 0.0-1.0 (default: 0.5) |
| `observed_at` | string | No | When the fact held or the event happened ([#2330](https://github.com/kagura-ai/memory-cloud/issues/2330)): an ISO 8601 date (`2024-03-07`) or datetime, stored in UTC as `details.observed_at`. If `details.observed_at` is also given, the two must agree. An invalid value is `422` |

**Response:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "user_123",
  "summary": "User prefers dark mode in development tools",
  "type": "preference",
  "importance": 0.8,
  "created_at": "2025-11-22T10:30:00Z"
}
```

**Example (Python):**

```python
import requests

response = requests.post(
    "http://localhost:8080/api/v1/memory/remember",
    headers={"Authorization": "Bearer kagura_xxxxxxxxxxxx"},
    json={
        "summary": "FastAPI async best practices",
        "content": "Use async/await for I/O operations, asyncpg for PostgreSQL...",
        "type": "code",
        "tags": ["python", "fastapi", "async"],
        "importance": 0.9
    }
)
print(response.json())
```

---

### Chunking Best Practices

For long documents or code files, create **multiple semantic memories** instead of storing everything in one memory. This improves search quality and follows RAG best practices (optimal chunk size: 100-500 characters for summary).

#### ✅ Good Example: Long Code File

Instead of storing an entire 5000-line file:

```python
# ❌ BAD - Entire file in one memory
requests.post(
    "http://localhost:8080/api/v1/memory/remember",
    headers={"Authorization": "Bearer kagura_xxx"},
    json={
        "summary": "auth.py file",
        "content": "<entire 5000-line file>",
        "type": "code"
    }
)
```

Split by logical modules:

```python
# ✅ GOOD - Semantic chunks with meaningful summaries

# Chunk 1: OAuth2 login
requests.post(
    "http://localhost:8080/api/v1/memory/remember",
    headers={"Authorization": "Bearer kagura_xxx"},
    json={
        "summary": "OAuth2 login implementation using FastAPI",
        "content": "def oauth2_login(provider: str): ...",
        "tags": ["auth", "oauth2", "login"],
        "context": {"file": "backend/src/auth.py", "lines": "10-45"},
        "importance": 0.8,
        "type": "code"
    }
)

# Chunk 2: JWT validation
requests.post(
    "http://localhost:8080/api/v1/memory/remember",
    headers={"Authorization": "Bearer kagura_xxx"},
    json={
        "summary": "JWT token validation with expiry check",
        "content": "def validate_jwt(token: str) -> dict: ...",
        "tags": ["auth", "jwt", "validation"],
        "context": {"file": "backend/src/auth.py", "lines": "47-82"},
        "importance": 0.9,
        "type": "code"
    }
)

# Chunk 3: Session management
requests.post(
    "http://localhost:8080/api/v1/memory/remember",
    headers={"Authorization": "Bearer kagura_xxx"},
    json={
        "summary": "Session management utilities for Redis",
        "content": "class SessionManager: ...",
        "tags": ["auth", "session", "redis"],
        "context": {"file": "backend/src/auth.py", "lines": "84-150"},
        "importance": 0.7,
        "type": "code"
    }
)
```

**Benefits**:
- Each memory has a semantic summary (searchable)
- Common tags (`["auth"]`) link related memories
- `context` object provides file location
- `recall("JWT validation")` finds the right memory

#### ✅ Good Example: Long Document

Instead of storing an entire research paper:

```bash
# ❌ BAD - Entire paper
curl -X POST http://localhost:8080/api/v1/memory/remember \
  -H "Authorization: Bearer kagura_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "RAG paper",
    "content": "<entire 20-page paper>",
    "type": "note"
  }'
```

Split by sections:

```bash
# ✅ GOOD - Introduction section
curl -X POST http://localhost:8080/api/v1/memory/remember \
  -H "Authorization: Bearer kagura_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "RAG systems: Introduction and motivation",
    "context_summary": "Explains why RAG is needed for LLMs. Covers limitations of pure parametric models.",
    "content": "<introduction section text>",
    "tags": ["RAG", "LLM", "paper-2024"],
    "context": {"paper_id": "rag-2024", "section": "intro", "pages": "1-3"},
    "importance": 0.7,
    "type": "learning"
  }'

# ✅ GOOD - Methodology section
curl -X POST http://localhost:8080/api/v1/memory/remember \
  -H "Authorization: Bearer kagura_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "summary": "RAG systems: Hybrid search methodology",
    "context_summary": "Describes hybrid search combining semantic (60%) and BM25 (40%). Includes chunking strategies.",
    "content": "<methodology section text>",
    "tags": ["RAG", "hybrid-search", "paper-2024"],
    "context": {"paper_id": "rag-2024", "section": "methods", "pages": "4-8"},
    "importance": 0.9,
    "type": "learning"
  }'
```

**Linking strategies**:
1. **Common tags**: `["paper-2024", "RAG"]` across all sections
2. **Context object**: `{"paper_id": "rag-2024", "section": "intro"}`
3. **Context overlap**: Mention related sections in `context_summary`

See [Chunking Guide](chunking-guide.md) for comprehensive examples and anti-patterns.

---

### POST /api/v1/memory/recall

Search memories using Hybrid Search (60% semantic + 40% BM25) with optional Neural Memory boosting.

**Request Body:**

```json
{
  "query": "How do I implement authentication in FastAPI?",
  "k": 10,
  "filters": {
    "context_id": "550e8400-e29b-41d4-a716-446655440000",
    "type": "code",
    "tags": ["python", "auth"],
    "importance": {"gte": 0.7}
  },
  "use_rerank": false
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Natural language search query |
| `k` | integer | No | Number of results (default: 5, max: 100) |
| `filters` | object | No | `context_id` (UUID) names the context to search — required. Also filter by type, tags, importance, date ranges. `tags_match: "all"` for AND logic. Date: `created_after`, `created_before`, `updated_after`, `updated_before` (ISO 8601). Event time ([#2330](https://github.com/kagura-ai/memory-cloud/issues/2330)): `observed_after` (inclusive) / `observed_before` (exclusive) on `details.observed_at`, ISO 8601 date or datetime; a date means the start of that day (UTC) and a memory without one never matches. **Trust:** `trust_tier: "trusted"` excludes `external`-tier contexts and `connector`-sourced memories — pass it for behaviour-influencing reads (see [Trust tier](concepts.md#agent-memory-substrate)) |
| `use_rerank` | boolean | No | Request reranking (default: false). Only effective if reranking is also enabled in the context's search config and a provider (Voyage/Cohere) is configured. |
| `full` | integer | No | How many of the top results carry `content` (default `0` — summaries only — on REST; `0`-`100`, never more than `k`; the MCP `recall` tool defaults to 3). Each is cut at 1,500 characters; a cut one carries `content_truncated: true` and `content_next_offset` (the `content_offset` that continues it in `reference`). Results past the first `full` carry no `content` key. The share-key `/recall` ignores it and returns summaries only. |

Errors: `422` for a missing or malformed `filters.context_id`; a context the caller may not read (unknown, other workspace, private non-creator, not a member, suspended, outside the member's context whitelist, or outside a workspace-scoped key's workspace) is the uniform `404` `Context not found` — the same check as `/memory/list`, `/memory/stats` and the MCP `recall` tool.

**Response:**

```json
{
  "results": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "summary": "FastAPI OAuth2 implementation guide",
      "context_summary": "Detailed walkthrough of OAuth2 setup...",
      "score": 0.95,
      "created_at": "2025-11-20T15:00:00Z"
    }
  ],
  "total": 1
}
```

With a reranker, `score` is the reranker's relevance; a candidate it could not score is placed after the scored ones and reported at or below the lowest scored one ([#2084](https://github.com/kagura-ai/memory-cloud/issues/2084)).

**Example (curl):**

```bash
curl -X POST http://localhost:8080/api/v1/memory/recall \
  -H "Authorization: Bearer kagura_xxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "neural memory implementation",
    "k": 5,
    "filters": {"context_id": "550e8400-e29b-41d4-a716-446655440000"},
    "use_rerank": false
  }'
```

---

### POST /api/v1/memory/reference

Retrieve complete details (Layer 3) of a specific memory by ID.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `memory_id` | string (UUID) | Yes | Memory ID from recall results |
| `version` | integer ≥ 1 | No | An earlier version to read ([#2043](https://github.com/kagura-ai/memory-cloud/issues/2043)); omitted, the current one |
| `record_access` | boolean | No | Default `true`: the read counts in the memory's access stats (`access_count`, the adoption signal `reference_count`, `last_used_at`). `false` reads without them, for a client's own re-read that is not an adoption ([#2518](https://github.com/kagura-ai/memory-cloud/issues/2518)); the access check and the response are unchanged |

**Response:**

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "summary": "FastAPI OAuth2 implementation guide",
  "content": "Full content of the memory...",
  "context_summary": "Detailed context...",
  "details": {
    "library": "Authlib",
    "version": "1.3.0"
  },
  "type": "code",
  "tags": ["python", "oauth2"],
  "importance": 0.9,
  "created_at": "2025-11-20T15:00:00Z",
  "updated_at": "2025-11-20T15:00:00Z",
  "observed_at": null,
  "version": 1,
  "historical": false,
  "current_version": null,
  "unavailable_fields": null,
  "partially_unavailable_fields": null
}
```

`observed_at` ([#2330](https://github.com/kagura-ai/memory-cloud/issues/2330)) is `details.observed_at` (a date or a UTC datetime, when the fact held), or `null`; an earlier version shows its own value.

`version` ([#2043](https://github.com/kagura-ai/memory-cloud/issues/2043)) is 1 when the memory is created and moves by one with each edit that changes `summary`, `context_summary`, `content`, `details`, `type`, `importance` or `tags`; `remember` and `recall` results carry it too. Pass it as `expected_version` to `PATCH` below.

`supersede_candidate` ([#1403](https://github.com/kagura-ai/memory-cloud/issues/1403)), when the memory carries a live suggestion, adds `acceptance: {allowed, reason?}` here and on the `PATCH` reply below ([#2427](https://github.com/kagura-ai/memory-cloud/issues/2427)): whether you may accept it (write the `supersedes` edge). `reason` is present only when `allowed` is false, in this order: `not_writable` (you have no write access to the context; for an agent-bound API key, also a binding that does not grant writes there; on a memory that carries a tool guardrail (`details.tool_trigger`), also a role below context editor or an agent-bound API key, which the accept refuses as any edit of a guardrail ([#2614](https://github.com/kagura-ai/memory-cloud/issues/2614))), `not_owner` (one or both memories are not your own; a linked account counts only in a private context), `newer_candidate` (the candidate was written after this memory), `edge_exists` (a different declared edge kept by the memory's author, the account the accept writes under, already links the pair, so the accept would return a 409 with `details.reason` = `edge_exists`: keep that edge, or, as its author, replace it with `POST /api/v1/graph/edges` and `overwrite: true`; an edge kept by another account, a linked caller's own included, does not refuse it ([#2549](https://github.com/kagura-ai/memory-cloud/issues/2549), [#2614](https://github.com/kagura-ai/memory-cloud/issues/2614))) ([#2518](https://github.com/kagura-ai/memory-cloud/issues/2518)). `recall` rows carry it too ([#2508](https://github.com/kagura-ai/memory-cloud/issues/2508)), except on the share-key `/recall`, whose reader cannot accept anything. The server still checks write access and ownership when the edge is written; the `newer_candidate` rule is the one `update_memory(accept_supersede_candidate=true)` enforces, while `POST /api/v1/graph/edges` writes any pair you own in either direction.

With `version` below the current one, the response is that earlier version, rebuilt from the memory's revision log: `historical: true`, `current_version` is the live version, `updated_at` is when that version was written (`created_at` for version 1), and `outgoing_links` / `incoming_links` / `supersede_candidate` come back empty because they describe the memory now. `unavailable_fields` names fields whose old value account erasure removed (they read `null`). `partially_unavailable_fields` ([#2389](https://github.com/kagura-ai/memory-cloud/issues/2389)) maps a field to how many of its top-level keys account erasure removed from that version, e.g. `{"details": 2}`: the value shows the other keys (`{}` when it removed every key) and is not `null`; the removed keys are not named. Both are `null` when nothing is missing. A `version` above the current one is `422`; a forgotten memory is `404`, as for every read. [MCP Tools › `list_revisions`](mcp-tools.md#list_revisions-and-earlier-versions) explains what a rebuilt version contains.

**Example (Python):**

```python
response = requests.post(
    "http://localhost:8080/api/v1/memory/reference",
    headers={"Authorization": "Bearer kagura_xxxxxxxxxxxx"},
    json={"memory_id": "550e8400-e29b-41d4-a716-446655440000"},
)
print(response.json()["content"])
```

---

### GET /api/v1/memory/{memory_id}/revisions

A memory's revision log, newest first ([#2043](https://github.com/kagura-ai/memory-cloud/issues/2043)): one item per write event, with the version right after it.

**Query parameters:** `limit` (1–100, default 20), `cursor` (the `next_cursor` of the previous page).

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "current_version": 3,
  "revisions": [
    {"version": 3, "kind": "updated", "at": "2025-11-22T09:00:00Z", "changed_fields": ["importance"], "writer": "api", "by_you": true, "agent_id": null, "session_id": null},
    {"version": 2, "kind": "updated", "at": "2025-11-21T10:00:00Z", "changed_fields": ["summary", "tags"], "writer": "api", "by_you": false, "agent_id": null, "session_id": null},
    {"version": 1, "kind": "created", "at": "2025-11-20T15:00:00Z", "changed_fields": [], "writer": "api", "by_you": true, "agent_id": null, "session_id": null}
  ],
  "has_more": false,
  "next_cursor": null
}
```

`kind` is `created`, `updated`, `forgotten` or `restored`; `changed_fields` names the fields an `updated` event changed, and is empty when you may not read the version before or after the edit (`POST /reference` with that `version` answers `404`, as under a binding's memory-type filter). No member identity is returned: `by_you` marks events you (or an account linked to you) wrote, and only those carry `agent_id` / `session_id`. Read a version's text with `POST /reference` and `version`. Permission is the same as `reference`; `404` for no such memory, no access, or a forgotten memory; `422` for a `limit` out of range or a malformed `cursor`.

---

### PATCH /api/v1/memory/{memory_id}

Partial update: send any of `summary`, `content`, `type`, `importance`, `tags`, `details`, `observed_at`, `add_tags`, `remove_tags`, `details_patch`; omitted fields keep their value, `tags` replaces the list and `"details": null` clears it. `observed_at` ([#2330](https://github.com/kagura-ai/memory-cloud/issues/2330)) sets `details.observed_at`: alone, it keeps the rest of the stored details (a new version); with `details` or `details_patch` it must match their `observed_at` if that is given; `null` means unchanged. The response is the updated memory in the `reference` shape, `version` included.

`add_tags` / `remove_tags` and `details_patch` ([#2585](https://github.com/kagura-ai/memory-cloud/issues/2585)) edit the stored values instead of replacing them, as on `update_memory`: removals first, then each added tag not already present is appended once; `details_patch` merges into the stored details (a `NULL` column starts from `{}`) and a key set to `null` is deleted. The resulting list may hold at most 100 tags and each added tag at most 64 characters; a longer stored tag (one written through MCP, which allows 100) is kept. The reserved `details` keys are validated as in `details`. `tags` with `add_tags` / `remove_tags`, `details` (`null` included) with `details_patch`, or an explicit `null` for any of the three is `422`.

```json
{"remove_tags": ["status:open"], "add_tags": ["status:done"]}
```

`expected_version` (optional, integer ≥ 1) makes the patch conditional: it applies only while the memory is still at that version. Otherwise nothing is written and the call answers `409`:

```json
{
  "error": "RES-002",
  "message": "Memory 550e8400-… is at version 4, not the expected version 3. Read it again and resend with its current version.",
  "details": {"reason": "version_conflict", "expected_version": 3, "current_version": 4}
}
```

Read the memory again (`POST /reference`) and decide. Two patches racing with the same `expected_version` serialize on the row: one applies, the other gets the `409`. Without `expected_version` the last writer wins. `404`: no such memory or no access; `410`: the memory was forgotten; `422`: an invalid body (a body with only `expected_version` is one).

`accept_supersede_candidate` and `dismiss_supersede_candidate` ([#2600](https://github.com/kagura-ai/memory-cloud/issues/2600), JSON booleans, default `false`) accept or reject the memory's current `supersede_candidate` with the rules of [`update_memory`](mcp-tools.md#update_memory): `{"accept_supersede_candidate": true}` writes the declared `supersedes` edge from this memory to the candidate and clears the suggestion — the web app's accept. The edge is kept under the author of the memory carrying the suggestion (in a private context an account linked to yours may be that author), the account supersede detection looks the pair's edge up under, unlike `POST /api/v1/graph/edges`, which keeps an edge under the caller. Either flag set to `true` is a patch on its own; both together, or an accept with any field edit (`"details": null` included), is `422`. A dismissal may share the patch with edits, and `expected_version` applies to both. An accept with no live suggestion, with a memory that is not your own at either end, or with a candidate written after this memory is `422` and writes nothing; a different declared edge already linking the pair is `409` with `details.reason` = `edge_exists` and `details.existing_edge`; an endpoint that stops being yours between resolving the suggestion and writing the edge is `403`. An accept or dismissal-only patch leaves `version` and `updated_at` alone; the reply shows the memory without the suggestion (and, after an accept, the new edge in `outgoing_links`).

Every write records its session ([#2044](https://github.com/kagura-ai/memory-cloud/issues/2044)): a REST `PATCH` (and the other memory writes) takes it from the `gen_ai.conversation.id` / `session.id` baggage key of the request, the agent from the API key's agent binding only — a `gen_ai.agent.id` baggage value never becomes the writer's agent. MCP `changes_since(session_id=...)` lists one session's writes and `rollback_session` undoes them ([MCP Tools](mcp-tools.md#rollback_session)); there is no REST route for either.

---

### DELETE /api/v1/memory/forget

Permanently delete a memory by ID or search query.

**Request Body (by ID):**

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Request Body (by query):**

```json
{
  "query": "outdated test data",
  "k": 10
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `memory_id` | string (UUID) | No* | Specific memory to delete |
| `query` | string | No* | Search query to find memories to delete |
| `k` | integer | No | Max number to delete (default: 10, safety limit) |

*One of `memory_id` or `query` is required.

**Response:**

```json
{
  "deleted_count": 3,
  "message": "Successfully deleted 3 memories"
}
```

---

### POST /api/v1/memory/forget-bulk

Delete every memory in one context that matches a filter. Filters combine with AND.
`dry_run` defaults to `true`, so a first call only counts.

**Request Body:**

```json
{
  "context_id": "550e8400-e29b-41d4-a716-446655440000",
  "created_before": "2026-01-01T00:00:00Z",
  "type": "note",
  "tags": ["scratch"],
  "dry_run": true
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_id` | string (UUID) | Yes | The context to delete from |
| `created_before` | string (ISO 8601) | No* | Only memories created before this instant (exclusive) |
| `created_after` | string (ISO 8601) | No* | Only memories created at or after this instant (inclusive) |
| `type` | string | No* | Only memories of this type |
| `tags` | string[] | No* | Only memories carrying any of these tags |
| `all` | boolean | No* | Match every memory in the context |
| `dry_run` | boolean | No | Count without deleting (default: `true`) |
| `cursor` | string | No | The previous response's `next_cursor` (ignored on a dry run) |

*At least one filter, or `"all": true`, is required — an empty body is refused with 422.

Permissions are the same as `/forget`: write access to the context (404 otherwise), and a
memory the caller may not delete is skipped and not counted. Each deleted memory is removed the
way `/forget` removes one (soft delete, search index point and graph edges), in batches of 500,
each committed and audited on its own. One request deletes at most 2,000 memories: when more
match, the response says `"remaining": true` and carries `next_cursor` — repeat the same request
with `"cursor": "<next_cursor>"` until `remaining` is `false`. The cursor is opaque; it resumes after
the last memory deleted, so memories the caller may not delete are not scanned again.
Allowed while the workspace is over its Free plan's capacity (`CAPACITY-001`).

**Response:**

```json
{ "status": "success", "dry_run": true, "matched": 120, "deleted": null }
```

With `"dry_run": false` the response carries `deleted`, `remaining` and `next_cursor` instead of `matched`
(a dry run always reports the full match count).

---

### POST /api/v1/memory/explore

Discover related memories through Neural Memory graph traversal using activation spreading.

**Request Body:**

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "depth": 2,
  "min_weight": 0.5,
  "relation_types": ["related_to", "caused_by"]
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `memory_id` | string (UUID) | Yes | Seed memory ID to start exploration |
| `depth` | integer | No | Max hops in graph (default: 2, max: 5) |
| `min_weight` | float | No | Min edge weight (default: 0.5, range: 0.0-1.0) |
| `relation_types` | array[string] | No | Filter by relation types |

**Response:**

```json
{
  "explored_memories": [
    {
      "id": "650e8400-e29b-41d4-a716-446655440001",
      "summary": "Neural network activation functions",
      "relation": "related_to",
      "weight": 0.85,
      "distance": 1
    }
  ],
  "total": 1
}
```

---

### POST /api/v1/memory/guardrails

Deterministically load a context's guardrail set — the REST twin of the MCP `load_guardrails` tool and `/pinned`'s sibling. The trusted-tier pinned set (`delivery_mode="always"`) plus the memories marked with `details.tool_trigger`, each list ordered `importance DESC, created_at ASC, id ASC` and capped on its own. Plain SQL: no ranking, no rerank. The stored patterns are returned as data and never run on the server. Contract and cache format: [MCP Tools › Tool guardrails](mcp-tools.md#tool-guardrails).

**Request Body:**

```json
{
  "context_id": "550e8400-e29b-41d4-a716-446655440000",
  "cap": 50
}
```

`cap` (1–1000, default 50) bounds `tool_triggered` only; the pinned list is bounded by the server's `pinned_load_cap`.

**Response:**

```json
{
  "status": "success",
  "format": 1,
  "version": "3f9c1a7b2d4e6f80",
  "pinned": [
    {
      "memory_id": "…", "summary": "…", "context_summary": "…", "type": "note",
      "importance": 0.9, "delivery_mode": "always", "tool_trigger": null,
      "source_type": "manual", "authored_by_caller": true,
      "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"
    }
  ],
  "tool_triggered": [
    {
      "memory_id": "…", "summary": "…", "context_summary": null, "type": "troubleshooting",
      "importance": 0.8, "delivery_mode": "on_recall",
      "tool_trigger": {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge\\b.*--delete-branch", "action": "inform"},
      "source_type": "manual", "authored_by_caller": true,
      "created_at": "2026-09-02T00:00:00Z", "updated_at": "2026-09-02T00:00:00Z"
    }
  ],
  "total_available": 2, "truncated": false, "cap": 50,
  "pinned_cap": 100, "pinned_total_available": 1, "pinned_truncated": false,
  "tool_triggered_total_available": 1, "tool_triggered_truncated": false
}
```

Errors: `422` for a malformed `context_id` or an invalid `cap`; a context the caller may not read is the uniform `404` `Context not found`.

Writes that add, change or remove `details.tool_trigger` (and any edit or delete of a memory that carries one) need context editor or above: `POST /remember`, `PATCH /{memory_id}` and the MCP write tools return `403` / `permission_denied` otherwise, and `DELETE /forget` skips the guardrail (`deleted_count: 0` by `memory_id`; a `query` sweep leaves it out of its count). Validation failures are `422` with `invalid details.tool_trigger: <code>: …`.

---

### GET /api/v1/memory/guardrails/digest

Render a context's tool guardrails for MCP clients without tool hooks — the export block a Codex cloud setup script writes into `AGENTS.md`, or a preview of the MCP server `instructions` this credential would receive. Same trusted-only read and agent-binding filter as `/guardrails`; summaries only, never `content`, `details` or patterns. Lane description: [MCP Tools › Server instructions](mcp-tools.md#server-instructions).

**Query parameters:**

| Parameter | Meaning |
|---|---|
| `context_id` (required) | Context UUID; malformed → `422` `context_id must be a valid UUID: …` |
| `target` | `export` (default) → `text/markdown`, the whole block; `instructions` → `text/plain`, exactly the `instructions` string the MCP endpoint serves this credential for this context. Anything else → `422` |
| `profile`, `tools`, `add_tools` | `target=instructions` only: the MCP URL's values, so the truncation suffix names the same tool the URL's `tools/list` shows |

**Response (`target=export`):**

```
<!-- kagura-memory:guardrails begin context=550e8400-e29b-41d4-a716-446655440000 tool_triggered_version=3f9c1a7b2d4e6f80 -->
- (3f9c1a7b) Squash-merge only after gh pr view --json headRefOid equals the pushed SHA.
- (a1b2c3d4) gh pr merge --delete-branch on a stacked parent closes the child PR; retarget the child first.
<!-- kagura-memory:guardrails end -->
```

Up to 20 entries, each summary flattened to one line and cut at 500 characters on a word boundary with `…`; the block is at most 12,000 characters including the markers, LF line endings, a trailing newline, exactly one begin and one end line (a summary containing `<!--` or `-->` is written as `<!- -` / `- ->`, so it can never forge a marker). When entries are left out the last content line is `(+N more: get_context_info(context_id))`. A context with no tool guardrails — or an external-tier context — is a `200` with an empty body: nothing to write (the Codex cloud recipe removes an earlier block on it).

Headers: `Content-Type: text/markdown; charset=utf-8` (`text/plain; charset=utf-8` for `instructions`), `Cache-Control: private, no-store`, `X-Content-Type-Options: nosniff`, `X-Kagura-Guardrails-Tool-Triggered-Version: <hash>` — `guardrail_version` over the tool-triggered items only, after the binding filter, over the whole set up to `guardrail_load_cap` rather than the rendered entries ([Shared cache format](mcp-tools.md#shared-cache-format-format-1)); never the bare `version`, which also covers the pinned list.

Errors: `422` for a malformed `context_id` or an unknown `target`; a context the caller may not read (unknown, other workspace, private non-creator, not a member) is the uniform `404` `Context not found`. Auth: API key or session, like `/guardrails`; a workspace-scoped key is confined to its workspace and an agent-bound key to its bindings.

The Codex cloud setup-script recipe that consumes this endpoint is in [MCP Client Setup › Codex cloud](mcp-clients.md#codex-cloud).

---

## Context APIs

### GET /api/v1/contexts

List all contexts in the current workspace.

**Response:**

```json
{
  "contexts": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "my-project",
      "display_name": "My Project",
      "is_default": false,
      "is_private": true,
      "sleep_mode": "skip",
      "created_at": "2025-11-22T10:00:00Z"
    }
  ],
  "total": 1
}
```

---

### POST /api/v1/contexts

Create a new context.

**Request Body:**

```json
{
  "name": "my-project",
  "display_name": "My Project",
  "description": "Personal project notes",
  "is_private": true
}
```

**Response:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-project",
  "display_name": "My Project",
  "is_default": false,
  "is_private": true,
  "sleep_mode": "skip",
  "created_at": "2025-11-22T10:00:00Z"
}
```

---

### GET /api/v1/contexts/{context_id}

Get a single context by ID.

**Response:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-project",
  "display_name": "My Project",
  "description": "Personal project notes",
  "is_default": false,
  "is_private": true,
  "is_public": false,
  "is_locked": false,
  "sleep_mode": "skip",
  "created_at": "2025-11-22T10:00:00Z",
  "updated_at": "2025-11-22T10:00:00Z"
}
```

---

### PUT /api/v1/contexts/{context_id}

Update a context. All fields are optional.

**Request Body:**

```json
{
  "display_name": "Renamed Project",
  "description": "Updated description",
  "sleep_mode": "edges_only"
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `display_name` | string | No | Human-readable display name |
| `description` | string | No | Context description |
| `summary` | string | No | LLM-oriented context summary |
| `usage_guide` | string | No | The owner's notes on what the context holds and how it is organised (returned to AI clients as data, not instructions) |
| `is_private` | boolean | No | Privacy setting (owner-only) |
| `is_public` | boolean | No | Public API access flag (owner-only) |
| `resource_id` | string | No | Resource ID for public contexts (owner-only) |
| `is_locked` | boolean | No | Lock to prevent deletion (owner-only) |
| `sleep_mode` | string | No | Sleep maintenance mode: `full`, `edges_only`, or `skip` (owner-only) |

**Response:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "my-project",
  "display_name": "Renamed Project",
  "sleep_mode": "edges_only",
  "updated_at": "2025-11-22T10:30:00Z"
}
```

---

### DELETE /api/v1/contexts/{context_id}

Soft-delete a context. The context row is marked deleted (sets `deleted_at`) so it stops appearing in listings, but the record and its memories are retained for recovery / audit purposes.

**Response:** `204 No Content` (no response body)

---

### POST /api/v1/contexts/{context_id}/members

Give a workspace member a role in a shared context. Session only; the caller owns the context or is a workspace owner or admin.

**Request Body:**

```json
{
  "user_id": "member-user-id",
  "role": "editor",
  "extend_access_list": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_id` | string | Yes | A member of the context's workspace |
| `role` | string | Yes | `owner`, `editor` or `viewer` |
| `extend_access_list` | boolean | No | Default `false`. When the member's (or viewer's) context access list does not include this context, add the context to that list in the same transaction. Workspace owner or admin only (`403` otherwise) |

A context role does nothing for a member whose context access list does not include the context (see [Concepts](concepts.md)). Without `extend_access_list` such an add answers `409`:

```json
{
  "error": "RES-002",
  "message": "This member's context access list does not include this context, so a context role would give them nothing. A workspace owner or admin can add it to the list by resending with extend_access_list=true, or edit the member's context access first.",
  "details": {
    "reason": "context_not_in_access_list",
    "user_id": "member-user-id",
    "context_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

A context member row the member already holds but whose list does not reach the context (it grants nothing and is hidden from the context member list) is handled the same way: `409` without the flag; with it the list is extended and that row is kept at the requested role. Only a row that already grants the context answers `400` (already a context member).

**Response:** `201 Created` with `user_id`, `role`, `added_at` and `access_list_extended` (`true` when the context was added to the member's list).

---

## Workspace Member APIs

A member's **context access list** (`allowed_context_ids`) decides which contexts a member or viewer reaches ([concepts](concepts.md#workspace)):

| Role | Not set (`null`) | Empty (`[]`) | A list |
|------|------------------|--------------|--------|
| **Owner / Admin** | Every context (the list is ignored) | Every context | Every context |
| **Member** | No context until one is assigned | No context | Exactly these contexts |
| **Viewer** | Every shared context | No context | Exactly these contexts |

A private context stays open only to its creator, whatever the list says, and the list is kept when the member's role changes. Every id in a list must be a live context of the workspace: an id of another workspace, a deleted context or an unknown id is `400` `Invalid context IDs: [...]`, and two spellings of one id are stored once.

The endpoints below accept a session admin or owner, or a **workspace-owner API key** (#1164, #2326; a key bound to another workspace gets `404`, an admin's key `403`, and a deployment can switch owner-key member management off). OAuth access tokens are refused with `403`. A change made with an API key is written to the audit log.

### POST /api/v1/workspaces/{workspace_id}/members

Add an existing user as a member. An API key cannot assign `owner` (`422`).

**Request Body:**

```json
{
  "user_id": "user-123",
  "role": "member",
  "allowed_context_ids": ["550e8400-e29b-41d4-a716-446655440000"]
}
```

`allowed_context_ids` is optional: left out or `null`, the list is not set (the table above; a member then reaches no context until one is assigned). Unlike an invitation, which requires at least one context for a member or viewer, a direct add does not require a list.

**Response:** `201 Created`

```json
{
  "user_id": "user-123",
  "role": "member",
  "joined_at": "2026-11-04T10:00:00Z",
  "allowed_context_ids": ["550e8400-e29b-41d4-a716-446655440000"]
}
```

---

### PUT /api/v1/workspaces/{workspace_id}/members/{user_id}/context-access

Replace a member's context access list.

**Request Body:**

```json
{
  "allowed_context_ids": ["550e8400-e29b-41d4-a716-446655440000"]
}
```

`null` clears the list (not set), `[]` gives no context.

**Response:**

```json
{
  "status": "ok",
  "user_id": "user-123",
  "allowed_context_ids": ["550e8400-e29b-41d4-a716-446655440000"]
}
```

---

### GET /api/v1/workspaces/{workspace_id}/members/{user_id}/context-access

A member's context access list and the contexts it can be given: every live shared context of the workspace plus the member's own private contexts (including those of an account linked to the member, `is_own_private`), ordered by name. Another member's private context is never listed. A read: nothing is written to the audit log. `404` when `user_id` is not a member of the workspace. Set the list with `PUT` on the same path.

**Response:**

```json
{
  "user_id": "member-user-id",
  "role": "member",
  "allowed_context_ids": ["550e8400-e29b-41d4-a716-446655440000"],
  "contexts": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "team-notes",
      "display_name": "Team Notes",
      "summary": null,
      "is_private": false,
      "is_own_private": false
    },
    {
      "id": "6fa459ea-ee8a-4ca4-894e-db77e160355e",
      "name": "drafts",
      "display_name": null,
      "summary": null,
      "is_private": true,
      "is_own_private": true
    }
  ]
}
```

---

## Agent Control Plane APIs (v0.49.0 preview)

Agents are workspace-scoped registry resources, not principals. Registry and binding mutations require workspace `Owner` or `Admin`. Agent-bound member keys keep their existing RBAC ceiling; bindings are purely subtractive.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/agents` | Register an agent (`active`, `enforce` by default) |
| `GET /api/v1/agents` | List registered agents |
| `GET /api/v1/agents/{agent_id}` | Get one agent |
| `PATCH /api/v1/agents/{agent_id}` | Update metadata, lifecycle status, or enforcement mode |
| `DELETE /api/v1/agents/{agent_id}` | Permanently delete the agent and cascade agent-bound keys; prefer `status="retired"` operationally |
| `POST /api/v1/agents/{agent_id}/bindings` | Add a context binding |
| `GET /api/v1/agents/{agent_id}/bindings` | List bindings |
| `PATCH /api/v1/agents/{agent_id}/bindings/{binding_id}` | Update read/write/default policy |
| `DELETE /api/v1/agents/{agent_id}/bindings/{binding_id}` | Remove a binding |
| `POST /api/v1/agents/{agent_id}/bootstrap` | Compose context info, pinned, optional trusted recall, upcoming, and state for session start |

Owner-provisioned member keys are minted through `POST /api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys`; supplying `agent_id` attaches the registered agent. Agent-bound keys for `suspended` or `retired` agents fail verification. In `enforce` mode, requests to unbound contexts use the same not-found shape as inaccessible contexts.

For registered ranking evaluations, the optional `recall_evaluation` object accepts a
deterministic `seed`, an exact `exploration_floor`, and `candidate_pool_k` (1–100, at least
`recall_k`). The successful recall component then adds identity-only
`selection_probabilities` for the complete authorized trusted candidate pool and a stamped
`selection_policy`. Ordinary bootstrap clients are unchanged when the object is omitted;
component errors never include this evidence.

The bootstrap reply is held to `max_chars` characters (default 20,000, range 10,000–100,000):
over budget, `context_summary` is left out first (`context_summary_omitted: true`), then the
pinned, recall and upcoming lists are cut from the end, each flagged `truncated: true`.
`pinned_cap` defaults to 20 here, and the state component is the first `get_state` page
(`has_more` / `next_cursor`). Recall results are the MCP `recall` tool's `verbosity="full"`
items: an empty `context_summary`, `tags`, `superseded_by` or `contradicts` is absent, not
`null` / `[]`. Unlike `recall`, a bootstrap row never carries `supersede_candidate` or
`observed_weekday` (#2689: this REST response stays as it was); like it, a row
carries `folded: {count, memory_ids}` when judge-verified older versions were folded under it. See [the bootstrap contract](design/agent-bootstrap-contract.md).

> **Preview boundary:** `allowed_memory_types` and `allowed_source_types` are enforced per read-lane row as of [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299) (`null` = all, `[]` = deny-all) on the memory-read lanes (recall, reference, forget, explore, load_pinned, upcoming) for enforce-mode agents — `reference` with a `version` is judged on that version's type as well as the live one ([#2116](https://github.com/kagura-ai/memory-cloud/issues/2116)); shadow mode records `would_deny` without filtering. REST and MCP accept W3C `traceparent` and baggage keys `gen_ai.agent.id`, `gen_ai.conversation.id` (or `session.id`), and `kagura.agent.run.id`; invalid advisory values are dropped and credential-bound agent identity always wins. Server-side span export is not part of P0. The append-only `memory_access_events` table and writer cover bootstrap, load-pinned, feedback, recall, reference, remember, update, and forget emission with binding deny capture.

---

## Agent State APIs

A per-context key→value scratch lane for autonomous agent loops (current task, plan step, cursor). Stored in a dedicated `agent_states` table — **structurally excluded from `recall()`** so it never pollutes semantic search. Part of the [Agent Memory Substrate](concepts.md#agent-memory-substrate). Reads require `Viewer`; writes require `Editor`.

### PUT /api/v1/contexts/{context_id}/state/{key}

Set (upsert) a state value under a key.

**Request Body:**

```json
{
  "value": {"step": 3, "plan": "refactor auth"},
  "ttl_seconds": 3600
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `value` | any (JSON) | Yes | Arbitrary JSON value to store |
| `ttl_seconds` | integer | No | Time-to-live; clamped server-side to a 30-day max. Omit for no expiry |

**Response:** `{ "key": "<key>" }`

### GET /api/v1/contexts/{context_id}/state/{key}

Get one key's live value. Expired entries are reaped lazily and reported as not found.

**Response:** `{ "key": "<key>", "value": <json> }`

### GET /api/v1/contexts/{context_id}/state

List all live state entries for the context.

**Response:** `{ "states": { "<key>": <json>, ... }, "count": 2 }`

### DELETE /api/v1/contexts/{context_id}/state/{key}

Delete one entry.

**Response:** `{ "key": "<key>" }`

---

## Retrieval Feedback API

Record an explicit, attributable signal on whether a recalled memory was helpful. Stored **append-only** in a dedicated `retrieval_feedback` table (a time series — contradicting signals are kept), embedded nowhere and excluded from `recall()`. Recording is **read-adjacent**: any `Viewer` who consumes recall may rate it. See the [eval-gate policy](eval/retrieval-feedback-and-eval-gate.md) — feedback is collected now but **not acted on automatically** until the golden eval gate (#344) is green.

### POST /api/v1/contexts/{context_id}/feedback

**Request Body:**

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "helpful": true,
  "query": "how do I rotate JWT refresh tokens?",
  "note": "exact match, used verbatim"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `memory_id` | string (UUID) | Yes | The recalled memory being rated (must belong to the context) |
| `helpful` | boolean | Yes | Whether the memory was useful for the query |
| `query` | string | No | The originating query (max 1024 chars) |
| `note` | string | No | Free-text rationale (max 2000 chars) |

**Response:** `201 Created` — `{ "feedback_id": "<uuid>", "memory_id": "<uuid>", "helpful": true }`

### POST /api/v1/contexts/{context_id}/host-feedback

Record an independently verified outcome with server-stamped `host` provenance.
This endpoint is for trusted workspace owners/admins and operator automation only;
agent-bound API keys are always rejected. The public feedback endpoint above is
unchanged and can only produce `agent` provenance.

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "helpful": true,
  "query": "bootstrap task 07",
  "verdict_source": "objective_check",
  "verdict_reference": "pytest://bootstrap/task-07",
  "experiment_id": "bootstrap-ab-2026-07-16",
  "note": "all assertions passed"
}
```

`verdict_source` must be `objective_check`, `trusted_host_check`, or
`hitl_approval`; `verdict_reference` must identify the check/run/approval that
produced the verdict. The feedback event and its actor/context/memory/experiment
audit record are append-only and committed together.

Keep this operator credential outside the evaluated agent's process and prompt.
An evaluated model must never receive, read, log, or invoke the credential; the
trusted harness submits the verdict only after its independent check completes.

---

## Memory Analysis APIs

Memory Analysis clusters a context with UMAP + KMeans and labels clusters with the workspace owner's BYOK provider. It requires workspace `Owner`, Pro plan access, the configured allowlist/quota gates, and an active BYOK key.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/contexts/{context_id}/analyses/preview` | Estimate count/cost and validate the run without creating it |
| `POST /api/v1/contexts/{context_id}/analyses` | Start a run (`202 Accepted`) |
| `GET /api/v1/contexts/{context_id}/analyses` | List runs with cursor pagination |
| `GET /api/v1/contexts/{context_id}/analyses/active` | Return the most recent succeeded run |
| `GET /api/v1/contexts/{context_id}/analyses/{run_id}` | Get one run |
| `GET /api/v1/contexts/{context_id}/analyses/{run_id}/clusters` | List cluster summaries |
| `GET /api/v1/contexts/{context_id}/analyses/{run_id}/positions` | List per-memory 2D positions |
| `DELETE /api/v1/contexts/{context_id}/analyses/{run_id}` | Cancel a running analysis |

`ANALYSIS_MAX_MEMORY_COUNT` defaults to 10,000 and is enforced by preview, start, and the pre-materialization count probe. Since v0.47.0, cancellation is all-or-nothing and stops in-flight labeling, deleted-context runs are invisible across REST and MCP, the labeling path disallows platform-key fallback, and a run fails when more than `MAX_CLUSTER_FAILURE_RATIO` (0.5) of labelable clusters fail.

---

## Resource Ingest APIs

| Endpoint | Purpose / authentication |
|---|---|
| `POST /api/v1/resources/{resource_id}/events` | Ingest one event with a resource token |
| `POST /api/v1/resources/{resource_id}/events/batch` | Ingest up to 100 events with partial-success semantics and a resource token |
| `GET /api/v1/resources` | List resources visible to the authenticated workspace principal |
| `GET /api/v1/resources/{resource_id}/events` | Inspect a resource's event history |

Since v0.48.0, the REST batch endpoint and MCP `ingest_events` delegate to the same `ResourceIngestService`. Authentication and wire envelopes remain surface-specific, while quota, authoritative Resource resolution, UTF-8 byte-size validation, per-event SAVEPOINT handling, constraint mapping, commit behavior, and post-commit indexer scheduling share one implementation.

### Resource token management

Workspace owner only. Full usage in the [Resource Tokens Guide](resource-tokens-guide.md).

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/resource-tokens` | List the workspace's tokens (metadata only; `?resource_id=` narrows to one resource) |
| `POST /api/v1/resource-tokens` | Mint a token for a resource with a live context; the plaintext is returned once |
| `PATCH /api/v1/resource-tokens/{token_id}` | Change `description` and/or `quota_events_per_hour` |
| `DELETE /api/v1/resource-tokens/{token_id}` | Revoke (soft delete; the row stays listed as `revoked`) |

**The population is the workspace's** ([#1919](https://github.com/kagura-ai/memory-cloud/issues/1919)). A token belongs to the workspace when the `resources` row its `resource_pk` points at does; a legacy row without `resource_pk` belongs through its own `workspace_id` column. The list and the `PATCH` / `DELETE` lookup use that attribution over all of the workspace's tokens, revoked ones included (a revoked token can still be relabelled or revoked again). The two limits are taken over the subset that counts: the workspace's **active** tokens, whoever minted them, excluding connector-owned ones (a connector's token is gated by `max_connectors` seats, not by this cap). MCP `setup_resource` and the downgrade-eligibility read count that same subset.

- **Count cap** (`POST`): the number of tokens in that set must be under the plan's `max_resource_tokens` (M/basic 3, L/pro 30, XL/promax 150; only XL may mint, [#1551](https://github.com/kagura-ai/memory-cloud/issues/1551)). Refused with `403` `QUOTA-001` carrying `current` and `limit`. Before #1919 the count was per creator, so two owners could each mint a full set.
- **Quota ceiling** (`PATCH` raising `quota_events_per_hour`): the sum of `quota_events_per_hour` over that set, including the new value, must not exceed `max_resource_tokens × 10,000` events/hour. Refused with `400`. Lowering a quota is never refused. Because each token is at most 10,000 events/hour, a workspace within its count cap is always within its ceiling, so creation does not check the sum.
- **Lookup** (`PATCH`, `DELETE`): the token is resolved with the same predicate, so any token that counts can be updated and revoked. A token of another workspace, or a row whose `resource_pk` points at another workspace's resource, answers the same `404` as an unknown id.

MCP `list_resource_tokens` lists the tokens that can authenticate — those with a `resources` row — and leaves out legacy rows without `resource_pk` (they cannot authenticate ingest, since `verify_token` joins through `resource_pk`). The REST list includes them so an owner can still revoke or relabel them.

### Public MCP endpoint publication

Workspace owner only (session or owner API key). Publishes a resource's public context as the anonymous read-only MCP endpoint `/mcp/public/<public_id>` ([#2675](https://github.com/kagura-ai/memory-cloud/issues/2675)); usage in the [Resource Tokens Guide](resource-tokens-guide.md#publishing-a-resource-as-a-public-mcp-endpoint), the MCP surface in [MCP Tools › Public endpoint tools](mcp-tools.md#public-endpoint-tools).

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/resources/{resource_id}/public-mcp` | Publish with `{public_name, public_description?, public_usage_guide?}` → `201`. `404` unknown resource; `422` with `details.precondition` `context_bound` (no live context for the resource) or `context_public` (the context is not public); `409` when the resource already has a live endpoint |
| `GET /api/v1/resources/{resource_id}/public-mcp` | The live endpoint, or `404` |
| `PATCH /api/v1/resources/{resource_id}/public-mcp` | Edit any of the three texts (`""` clears the description or usage guide); the address does not change |
| `DELETE /api/v1/resources/{resource_id}/public-mcp` | Unpublish → `204`; `404` when none is live. Publishing again mints a new address |
| `GET /api/v1/resources/{resource_id}/public-mcp/usage-health` | Usage event health ([#2678](https://github.com/kagura-ai/memory-cloud/issues/2678)): `{resource_id, endpoint_id, window_hours, events, outcomes, gaps: {count, dropped_events, last_gap_at}, writer: {enabled, queue_depth, queue_capacity, dropped_since_start, last_flush_at, last_flush_ok, process_local}, retention_days}`. `endpoint_id` is the live endpoint (`null` when unpublished); `events` / `outcomes` count its calls of the last 24 hours; `gaps` are the deployment's gap rows in that window (events the server could not record); `writer` is the answering API process only. `404` for a slug of another workspace. See [Public MCP usage events](public-mcp-usage-events.md) |

Response: `{public_id, endpoint_path, resource_id, context_id, public_name, public_description, public_usage_guide, created_at, updated_at, serving, usage_budget}`. `endpoint_path` is a path on this deployment; `serving` is true while `MCP_PUBLIC_ENDPOINTS_ENABLED` is on and the context is still public and bound to the resource.

`usage_budget` ([#2805](https://github.com/kagura-ai/memory-cloud/issues/2805)) is the **workspace-wide** seven-day budget of anonymous public calls, the same on every endpoint of the workspace: `{state, limit, period_days, used, remaining, period_started_at, resets_at, period_seq, as_of}`. `state` is `disabled` (operator switch `MCP_PUBLIC_USAGE_BUDGET_ENABLED` off; the numbers and times are `null`), `idle` (no period runs; `used` 0), `active` or `exhausted` (`used >= limit`). It does not change `serving`. Contract: [Public usage budget](public-usage-budget.md).

While custom domains are switched on (below), `DELETE` also releases the endpoint's custom domains in the same transaction: claims are deleted, held names are released with the reassignment cooldown. A republish gets no alias.

### Custom domains of a public MCP endpoint

Verified custom domains ([#2809](https://github.com/kagura-ai/memory-cloud/issues/2809)): an alias `https://<host>/mcp` that serves the same endpoint as its fixed address, after the owner proved control of the name with the DNS TXT record `_kagura-public-mcp.<host>` = `kagura-public-mcp-verification=<token>`. Every route answers `404` `RES-001` before any statement while the operator switch `MCP_PUBLIC_DOMAINS_ENABLED` is off, and sends `Cache-Control: no-store` (except the generic request-validation `422`: a body with no, an empty or a too long `hostname`, a `domain_id` that is not a UUID, a `resource_id` outside the slug pattern). Contract (states, right, lease, cooldown, limits): [Public MCP custom domains](public-mcp-domains.md).

| Endpoint | Who | Purpose |
|---|---|---|
| `GET /api/v1/resources/{resource_id}/public-mcp/domains` | workspace owner | `200` `{"domains": [domain, …]}`: the live endpoint's domains that are not released; `404` when the resource has no live endpoint |
| `POST /api/v1/resources/{resource_id}/public-mcp/domains` | canonical owner | Add `{hostname}` (1–255 characters; ASCII, `xn--` A-labels for international names) → `201` domain in state `pending`, with the TXT record to publish |
| `POST /api/v1/resources/{resource_id}/public-mcp/domains/{domain_id}/verify` | canonical owner | Look the records up now → `200` domain. A `pending` domain whose proof is found becomes `active` when the right is active and the name is free; an `active` or `suspended` one gets the periodic check's result |
| `DELETE /api/v1/resources/{resource_id}/public-mcp/domains/{domain_id}` | workspace owner | Remove → `204`. A `pending` domain is deleted, a held one released with the cooldown; the alias answers `404` from the next request on |

The **canonical owner** is the owner whose user id is the workspace's `owner_user_id`; another owner member can list and remove but gets `403` on add and verify.

Domain: `{domain_id, hostname, url, state, suspended_reason, serving, verification: {record_type: "TXT", record_name, record_value}, routing: {target, status}, last_check: {result, checked_at}, proof_ok_at, proof_missing_checks, serve_until, created_at, verified_at}`. `state` is `pending`, `active` or `suspended`; `suspended_reason` `proof_lost`, `right_ended`, `reserved` or `null`; `serving` says whether the alias answers right now (the alias statement resolves the domain and the name is still claimable; a plan pause does not change it); `routing.status` (`ok`, `mismatch`, `unresolved`) is informational; `last_check.result` is `ok`, `missing` or `unavailable`; `serve_until` is the lease (the alias is served only before it).

| Status | `error` | When |
|---|---|---|
| `404` | `RES-001` | switch off; no live endpoint for the resource; a `domain_id` that is not an open domain of this workspace and endpoint |
| `403` | `public_mcp_domain_owner_required` | add or verify by an owner member who is not the canonical owner (no counter, no lookup) |
| `403` | `public_mcp_domain_not_entitled` | `MCP_PUBLIC_DOMAINS_AUTHORITY=entitlement` and the workspace has no active `public-mcp-domains` right; on verify of an `active` domain it is suspended first |
| `422` | `public_mcp_domain_hostname_not_allowed` | malformed or reserved name; one code and one message ("This host name cannot be used as a custom domain.") for both |
| `409` | `public_mcp_domain_limit_reached` | the workspace already has `MCP_PUBLIC_DOMAINS_MAX_PER_WORKSPACE` domains (`pending`, `active` or `suspended`) |
| `409` | `public_mcp_domain_exists` | the endpoint already has a domain |
| `409` | `public_mcp_endpoint_not_published` | the endpoint was unpublished or replaced during verify |
| `409` | `public_mcp_domain_held` | another domain holds the name; answered only after the caller's own proof was found |
| `409` | `public_mcp_domain_in_cooldown` | another workspace released the name recently; `details.available_after` (ISO UTC, `Z`); answered only after the caller's own proof was found |
| `409` | `public_mcp_domain_changed` | the domain changed during the lookup; retry |
| `429` | `RATE-001` | more than 20 verify calls per workspace per hour, or 6 activation attempts per workspace per day; `Retry-After` header, `details.retry_after`, `details.limit` = `verify_per_hour` or `activations_per_day` |
| `503` | `public_mcp_domain_verification_unavailable` | the DNS lookup was indeterminate (nothing is written), or a limit counter could not be read |

**Certificate permission call.** `GET /internal/public-mcp-domains/tls-permission?domain=<name>` is for a TLS proxy that obtains certificates on demand; it is not in the OpenAPI document and carries no credential, so it must stay unreachable from outside, like every `/internal` route (under a Host that is neither the application's nor an internal one the API itself answers `404` for `/internal`). It answers with an empty body: `200` when the name is an `active` alias with a current lease of a live endpoint and both `MCP_PUBLIC_DOMAINS_ENABLED` and `MCP_PUBLIC_ENDPOINTS_ENABLED` are on; `404` for anything else (either switch off, no or several `domain` values, a malformed, special-use, application or reserved name, a name that is not served); `503` when it cannot answer now (4 lookups already run in this process, the lookup takes over 3 s, or a database error), which a proxy reads as a refusal. Plan pause and capacity lock do not withdraw the permission. The query of this path is redacted in every log line.

### Observation credentials and the observability API

Workspace owner only (session or owner API key) for the credential routes; the observability routes accept **only** an observation credential (`Authorization: Bearer kagura_obs_…`) and answer `404` while `OBSERVABILITY_API_ENABLED` is off ([#2679](https://github.com/kagura-ai/memory-cloud/issues/2679)). Bodies, cursors, limits and error codes: [Observability API](observability-api.md).

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/observation-credentials` | Issue `{name, scope: "workspace" \| "resource", resource_id?, ttl_days? (1-365, default 90)}` → `201` with the secret, shown once |
| `GET /api/v1/observation-credentials` | The workspace's credentials with `status` (`active` / `expired` / `revoked`) |
| `POST /api/v1/observation-credentials/{credential_id}/rotate` | `{grace_seconds? (0-86400)}` → `201` with a new secret of the same binding and remaining lifetime |
| `DELETE /api/v1/observation-credentials/{credential_id}` | Revoke → `204` |
| `GET /api/v1/observability/capabilities` | Scope, limits, schema versions, retention |
| `GET /api/v1/observability/resources` | Resources in scope with their live public MCP publication |
| `GET /api/v1/observability/snapshots` | Sanitised indexer state per resource (`never_run` / `idle` / `queued` / `running` / `error`) |
| `GET /api/v1/observability/events` | Public MCP usage events, commit-safe cursor |
| `GET /api/v1/observability/ingestion-events` | Payload-free, time-sealed summary of one resource's ingestion events |
| `POST /api/v1/observability/documents/resolve` | Up to 100 doc keys → current public title and source URL, or `null` |

### Connector create errors

`POST /api/v1/workspace-connectors` (workspace admin; MCP `setup_connector` raises the same errors). One platform team (for example a Slack workspace) can be connected to one Kagura workspace only; one workspace may connect it under several worker apps. A team that is already connected answers `409` `RES-002` with a stable `details.reason` ([#1753](https://github.com/kagura-ai/memory-cloud/issues/1753)):

| Status | `error` | `details.reason` | When |
|---|---|---|---|
| `409` | `RES-002` | `connector_team_connected_here` | This workspace already has a connector for the team under the same worker app. `details.connector_id` and `details.display_name` name it — edit that connector instead of creating another. When the request carried a `slack_install_handle`, that install's bot token is stored on the existing connector and the body adds `details.token_refreshed: true` ([#1880](https://github.com/kagura-ai/memory-cloud/issues/1880)); the key is absent otherwise (manual bind, or the token could not be stored) |
| `409` | `RES-002` | `connector_team_connected_elsewhere` | Another workspace has a connector for the team, under any worker app. The body carries nothing about that workspace (no id, name or owner) and does not echo the team id. To move the team, delete the connector in the original workspace; to share it, join that workspace |
| `409` | `RES-002` | — | `resource_id` already exists (not connector-owned, or already bound to a connector) |
| `403` | `FEAT-001` / `CONNECTOR-001` | — | Plan without the `connectors` feature / connector seat cap reached |

### Connector channel destinations

`PATCH /api/v1/workspace-connectors/{connector_id}` (workspace admin, [#1759](https://github.com/kagura-ai/memory-cloud/issues/1759)) takes two more optional fields next to `channel_ids`, `context_id` and the others, with the same PATCH semantics, `expected_config_version` lock (`409`) and `config_version` bump:

```json
{
  "channel_routes": [
    {"channel_id": "C0SALES", "action": "route", "context_id": "<context uuid>"},
    {"channel_id": "C0HR", "action": "skip"}
  ],
  "unrouted_private_channels": "skip",
  "expected_config_version": 7
}
```

- `channel_routes` is the full list: it replaces every stored route; `[]` or `null` removes them all; absent leaves them. `action` is `route` (default; needs `context_id`) or `skip` (do not ingest the channel; takes no `context_id`). At most 500 items, each `channel_id` once, by the `channel_ids` rule, and exactly as the platform names the channel: no surrounding whitespace or control characters, and on a Slack connector a conversation id (uppercase letters and digits, e.g. `C01ABCDEF`, never a `#name`), since the worker matches ids exactly. Routes do not depend on `channel_ids`. A route destination is a connector write target like the default context: a private context chosen as a destination serves the workspace from then on, so if its creator leaves the workspace or deletes their account it passes to the workspace owner with everything already in it instead of being deleted. The settings dialog says so on the row.
- `unrouted_private_channels`: `default` (a private channel without a route goes to the connector's context) or `skip` (it is not ingested). `null` is invalid.
- A channel with a route never falls back to the connector's context: when its destination is deleted, it is not ingested until an admin picks a new one (`status: "destination_missing"` in the read model).

| Status | `error` | When |
|---|---|---|
| `422` | `VAL-001` (`field: channel_routes` / `unrouted_private_channels`) | Malformed item; `CONNECTOR_CHANNEL_ROUTING_ENABLED` is off and the request creates or changes a route or sets the switch to `skip` (`channel routing is not enabled on this deployment`; clearing is always allowed); the connector has no write key or no default context; or the connector's key owner cannot write a destination (the message names the channel: the connector writes as its key's owner — pick a shared context or one that user created) |
| `404` | `RES-001` (`Context not found`) | A destination that is not a live context of this workspace you can read — the same answer whether it does not exist, is in another workspace, is in the trash, or is another member's private context |

The response, and each row of `GET /api/v1/workspace-connectors`, carries `unrouted_private_channels` and `channel_routes`: `{channel_id, action, context_id, context_name, context_is_private, status}` with `status` `ok` or `destination_missing`. A change is recorded in the operation history as `integration.updated` with `changed_fields: ["channel_routes"]` (field names only). The worker side is in [`docs/connector-ingest-contract.md`](connector-ingest-contract.md#worker-config-vend-per-channel-destinations-1759).

---

## Public Read API

`POST /api/v1/public/{context_id}/search` and `GET /api/v1/public/{context_id}/info` expose `is_public=true` contexts to anonymous and attributed callers (Issue #238, extended by Issue #626).

### Anonymous access (no Authorization header)

```bash
curl -X POST https://memory.example.com/api/v1/public/CTX_UUID/search \
  -H "Content-Type: application/json" \
  -d '{"query": "What is Hebbian learning", "limit": 3}'
```

Rate-limited to **50 requests/minute per context**, shared across all anonymous callers. The bucket is `public_search:{ctx}:minute` in Redis. The shared quota means a single noisy client can saturate the bucket — that's the gap public-bound API keys fill.

When the operator enables the public usage budget (`MCP_PUBLIC_USAGE_BUDGET_ENABLED`, [#2805](https://github.com/kagura-ai/memory-cloud/issues/2805)), an anonymous search of a context that has a live public MCP endpoint also spends that workspace's seven-day budget, shared with its public MCP endpoints: over it the search answers `429` `usage_budget_exhausted` with `Retry-After` (60–3600 s), and when the budget cannot be charged — or the per-minute bucket cannot be read — `503` `temporarily_unavailable` with `Retry-After: 60`. A request with a bound key is never charged; the route does not read the session cookie, so a signed-in request is bucketed, paused and charged like an anonymous one, and reranks only with a bound key ([#2823](https://github.com/kagura-ai/memory-cloud/issues/2823)). A context without a live publication is not counted. Contract: [Public usage budget](public-usage-budget.md).

### Attributed access (with public-bound API key)

```bash
curl -X POST https://memory.example.com/api/v1/public/CTX_UUID/search \
  -H "Authorization: Bearer kagura_xxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"query": "What is Hebbian learning", "limit": 3}'
```

When the bound key matches the URL context (CWE-639 IDOR guard fires otherwise with `403`), the request gets:

- **Per-key rate-limit bucket** (`public_bound_key:{key_id}:minute`) sized by the workspace plan's `bound_public_calls_per_minute` (PRO default: 100/min).
- **Per-key audit attribution** in `usage_stats` (`api_key_id` populated alongside `context_id` and `workspace_id`).
- **Independent revocation** — the key can be deleted without flipping `is_public=false` on the context.

The bound key MUST be created with `bound_context_id` set (see API Keys Management); a regular owner-scoped or workspace-scoped key passed here is rejected with `403`.

### Error matrix

| Condition | Status |
|---|---|
| Context not found | `404` |
| Context is not public | `403` |
| API key supplied but invalid / expired | `401` |
| API key is not public-bound | `403` |
| API key is bound to a different context (CWE-639) | `403` |
| Per-key / shared rate limit exhausted | `429` |
| Public usage budget exhausted (anonymous, published context, #2805) | `429` `usage_budget_exhausted` |
| Public usage budget could not be charged (#2805) | `503` `temporarily_unavailable` |

---

## OAuth2 APIs

### POST /api/v1/oauth/clients

Create a new OAuth2 client application.

**Request Body:**

```json
{
  "name": "My AI Application",
  "redirect_uris": ["https://myapp.com/callback"],
  "scopes": ["memory:read", "memory:write"],
  "grant_types": ["authorization_code", "refresh_token"]
}
```

**Response:**

```json
{
  "client_id": "client_abc123",
  "client_secret": "secret_xyz789",
  "name": "My AI Application",
  "created_at": "2025-11-22T10:00:00Z"
}
```

⚠️ **Important**: `client_secret` is shown only once. Store it securely.

---

### GET /api/v1/oauth/clients

List all OAuth2 clients for the authenticated user.

**Response:**

```json
{
  "clients": [
    {
      "client_id": "client_abc123",
      "name": "My AI Application",
      "scopes": ["memory:read", "memory:write"],
      "created_at": "2025-11-22T10:00:00Z"
    }
  ]
}
```

---

### DELETE /api/v1/oauth/clients/{client_id}

Delete an OAuth2 client.

**Response:**

```json
{
  "message": "Client deleted successfully"
}
```

---

### Authorization Code Grant (MCP clients)

Remote MCP clients register through Dynamic Client Registration
(`POST /api/v1/oauth/register`, always a public client) and sign in with the
authorization code grant and PKCE:

1. `GET /api/v1/oauth/authorize` with `response_type=code`, `client_id`,
   `redirect_uri`, `state`, `scope`, `code_challenge`,
   `code_challenge_method=S256` and `resource`. A signed-in user gets the
   consent page; anyone else is sent to sign in first. The consent page names
   the host the user is sent to after approving (with its port for a loopback
   redirect), and warns when every registered `redirect_uri` of the client is
   a loopback URI, since such a client's name cannot be verified.
   Each of `response_type`, `client_id`, `redirect_uri`, `scope`, `state`,
   `code_challenge` and `code_challenge_method` may be sent once; a repeated
   one gets the error page (`400`, `invalid_request`) on the GET and on the
   consent POST, without a redirect (a signed-out user is sent to sign in
   first, as for any request). `resource` may be repeated (RFC 8707);
   every value must name this server's MCP resource
   ([#2208](https://github.com/kagura-ai/memory-cloud/issues/2208)).
2. Approving redirects to `redirect_uri` with `code` and `state`. The consent
   page posts its form to `POST /api/v1/oauth/authorize` with the
   authorization request in the query string; the form sends only `confirm`,
   `default_context` and `csrf_token`, a token bound to the browser session.
   The OAuth parameters are read from the query only (form fields of the same
   name are ignored), and a missing or wrong `csrf_token` (for approving or
   denying) gets an error page (`400`, `invalid_request`) without a redirect
   or a stored code ([#2199](https://github.com/kagura-ai/memory-cloud/issues/2199)).
3. `POST /api/v1/oauth/token` with `grant_type=authorization_code`, `code`,
   `redirect_uri`, `client_id`, `code_verifier` and `resource`.
4. `POST /api/v1/oauth/token` with `grant_type=refresh_token` issues a new
   pair and revokes the old one.

**Body size.** `POST /api/v1/oauth/token`, the consent
`POST /api/v1/oauth/authorize`, `POST /api/v1/oauth/revoke` and
`POST /api/v1/oauth/introspect` refuse a form body over 16384 bytes, before it
is parsed, with `413` `invalid_request` (RFC 6749 JSON, `Cache-Control:
no-store`); the consent POST answers it without a redirect.

**Registration.** Each entry of `redirect_uris` is accepted on its own when
its host is `claude.ai`, `claude.com`, `anthropic.com`, `chatgpt.com`,
`chat.openai.com`, `platform.openai.com`, `cursor.sh` or `cursor.com` (or a
subdomain of one), or it is a loopback `http://localhost` / `127.0.0.1` /
`[::1]` URI and the `client_name` names a supported client, or (Gemini) it is
`https://oauth-redirect.googleusercontent.com`,
`https://oauth-redirect-sandbox.googleusercontent.com` or
`https://oauth-redirect-test.googleusercontent.com` exactly — no userinfo,
port, trailing dot or case variant, no subdomain — with the path
`/r/user_bound_custom-mcp-<one segment>` (RFC 3986 unreserved characters,
written as is or percent-encoded; no `..`, query, fragment or params), or (Muse) it is
`https://agent.meta.ai/api/hatch/oauth/callback`,
`https://muse.ai/api/hatch/oauth/callback` or
`https://platform.muse.ai/oauth/credential-test/callback` exactly, as written
([#2410](https://github.com/kagura-ai/memory-cloud/issues/2410),
[#2487](https://github.com/kagura-ai/memory-cloud/issues/2487)), or (Grok)
it is `https://grok.com/connectors-oauth-exchange-code/` exactly, as written,
trailing slash included
([#2779](https://github.com/kagura-ai/memory-cloud/issues/2779)), or (Google
connected apps) it is listed in `GOOGLE_CONNECTED_APPS_REDIRECT_URIS`, as
written ([#2773](https://github.com/kagura-ai/memory-cloud/issues/2773)). A
registration gets the provider `google_connected_apps` only when every entry
it keeps is listed there; listed entries sent next to any other qualifying
entry are left out. Such a client keeps its refresh token: a refresh answers
with the same `refresh_token` and a new access token, which replaces the
previous one; every other client's refresh rotates the pair. For that client,
`POST /api/v1/oauth/revoke` with the current access token revokes the refresh
token too, also when it races a refresh (the access token that refresh
issued is revoked with it); an access token already replaced before the
revocation arrives matches nothing and is answered `200`. Entries
that do not qualify are left out of the registration (RFC 7591 §3.2.1): the
response's `redirect_uris` lists what was registered, and only those are
accepted at `/authorize` and `/token`, so compare it with what you sent. A
registration with no qualifying entry is refused with `invalid_client_metadata`
([#2232](https://github.com/kagura-ai/memory-cloud/issues/2232)). An entry that
is not a redirect URI pattern at all (not an `http`/`https` URL with a host,
or a `*` other than one trailing `/*` after a non-empty path) still fails the
whole request with `422` before these checks. Each entry
left out is logged as host and path (userinfo, query and fragment dropped, the Gemini
binding segment masked, at most 10 per registration plus a count). Registered
redirect URIs are stored as sent and compared exactly at `/authorize` and
`/token`.
Registrations are limited to 5 per minute per client address (`429`
`invalid_request` over it). `OAUTH_DCR_RATE_LIMIT_EXEMPT_CIDRS`
(comma-separated CIDR ranges, empty by default) exempts callers in those
ranges, for example a connector platform's published egress range; behind a
reverse proxy, set `FORWARDED_ALLOW_IPS` so the address is the caller's.
A malformed range stops the server at startup.

**Loopback redirects.** A `redirect_uri` that is `http` on `localhost`,
`127.0.0.1` or `[::1]` matches a registered loopback URI on any port
(RFC 8252 §7.3), so a native client may listen on an ephemeral port. The
scheme, host, path and query must still match: `localhost` and `127.0.0.1`
are different hosts. Every other `redirect_uri` is matched exactly (or by
its registered trailing `/*`). The stored
scope is the requested scopes the server defines (`scopes_supported`). A
registration that omits `scope`, or whose defined scopes include no
`memory:*` scope (for example `claudeai` or `openid offline_access`), gets the
default scope, which leaves out `memory:admin`; a registration may name
`memory:admin` in its `scope`.

**Authorization request rules.** They are checked before the consent page is
shown and again when it is submitted. A request that breaks one gets an error
page naming the `error` (HTTP 400) instead of the consent page, and nothing is
sent to the client. If the submitted consent breaks one, the response
redirects to the registered `redirect_uri` with `error`, `error_description`
and `state` (RFC 6749 §4.1.2.1). An unregistered `redirect_uri` always gets an
error page and no redirect.

| Rule | `error` |
|---|---|
| `code_challenge_method` must be `S256`, the only value in `code_challenge_methods_supported`. `plain`, or a `code_challenge` sent without a method (which means `plain`, RFC 7636 §4.3), is refused. | `invalid_request` |
| A public client (`token_endpoint_auth_method=none`, which includes every DCR client) must send a `code_challenge`. A confidential client may leave PKCE out; if it sends one, the rule above applies. | `invalid_request` |
| Scope, see below. | `invalid_scope` for an admin-managed client registered without a `memory:*` scope |
| `resource` (RFC 8707), when sent, must name this server's MCP resource, see below. | `invalid_target` |

**Scope.** The granted scope is the requested scopes that the client
registered and that the server defines (`scopes_supported`); any other
requested scope is dropped (RFC 6749 §3.3). When that leaves no `memory:*`
scope (the request named none the client registered, for example only
`openid` / `offline_access` or scopes the server does not define, or had no
`scope`), the client's registered scope that the server defines is granted
instead. A DCR client whose stored scope has no `memory:*` scope is treated as
registered with the default scope. `memory:admin` is granted only to a client
that registered it. The consent page lists the permissions of the granted
scope, and the token response's `scope` carries it.

**Resource.** The MCP resource is the `resource` published at
`/.well-known/oauth-protected-resource` (`FRONTEND_URL` plus `MCP_BASE_PATH`,
for example `https://<your-domain>/mcp`). A `resource` names it when it has the
same scheme and host (compared case-insensitively after dropping a trailing
dot and IDNA encoding; the default port may be given or left out) and its path
is `MCP_BASE_PATH` or a path beneath it, such as `/mcp/`,
`/mcp/w/<workspace-id>` or `/mcp/sse`. Its query and fragment are
ignored (`/mcp?profile=full` names it). Any other origin or path is
`invalid_target`. Codes and tokens store the published value.

**Token requests.** Errors use the RFC 6749 §5.2 body
`{"error": "...", "error_description": "..."}`.

- A parameter the token endpoint reads (`grant_type`, `code`, `redirect_uri`,
  `client_id`, `client_secret`, `code_verifier`, `refresh_token`, `scope`,
  `device_code`) may be sent once; a repeated one gets `invalid_request`.
  `resource` may be repeated (RFC 8707), and every value must name this
  server's MCP resource.
- `code_verifier` must match the `S256` challenge (`invalid_grant` otherwise).
  A public client that leaves it out gets `invalid_request`. A code issued
  without a `code_challenge` is not exchanged with a `code_verifier`
  (`invalid_request`, RFC 9700 §4.8).
- An authorization code, a refresh token and a device code each yield one
  token. A second exchange of a code, or a second refresh with a refresh
  token, gets `invalid_grant`, also when both are sent at the same moment. A
  device code is deleted when its token is issued, so a second poll is
  refused.
- The authorization code carries the `resource` of the authorization request.
  The token's audience (`aud` in `POST /api/v1/oauth/introspect`) is that
  `resource`, or else the token request's. A token request whose `resource`
  does not name this server's MCP resource gets `invalid_target`. When
  neither request sends `resource`, the token has no audience.
- Refresh: the requested `scope` cannot go beyond the original grant
  (`invalid_scope`). The new token keeps the audience of the refreshed one; an
  audience stored in another form of the MCP resource (for example
  `.../mcp/w/<workspace-id>`) is stored as the published value. A `resource`
  on the refresh request must name the MCP resource (`invalid_target`
  otherwise). A token without an audience, refreshed with the MCP resource as
  `resource`, gets that audience. A token whose audience is not a form of the
  MCP resource keeps it when the refresh omits `resource`; a refresh naming
  the MCP resource gets `invalid_target`.

The token endpoint logs the grant type and the names of the parameters it
receives, not their values.

**Revocation and introspection.** Both endpoints take
`application/x-www-form-urlencoded` and authenticate the caller (#1741):

| Endpoint | Who may call | A token issued to another client |
|---|---|---|
| `POST /api/v1/oauth/revoke` (RFC 7009) | A public client by `client_id` (form, or HTTP Basic with an empty secret); a confidential client with its secret by HTTP Basic (`client_secret_basic`) or `client_id` + `client_secret` in the form (`client_secret_post`) | Left alone; the response is `200`, the same as for an unknown token |
| `POST /api/v1/oauth/introspect` (RFC 7662) | A confidential client with its secret, by either method. Public clients cannot introspect | `{"active": false}` |

A missing `client_id`, an unknown client or a wrong secret gets `401`
`invalid_client` with `WWW-Authenticate: Basic`. HTTP Basic together with a
form `client_secret`, or with a form `client_id` naming another client, gets
`400` `invalid_request`. Revoking a
refresh token also revokes the access token issued with it; revoking an
access token leaves its refresh token usable.

A form body over 16384 bytes gets `413` `invalid_request` on both endpoints,
before it is parsed and before the client is authenticated. A missing or
empty `token` gets `400` `invalid_request` (RFC 6749 JSON, `Cache-Control:
no-store`); before #2207 it was a `422` validation error.

---

### Device Authorization Grant (CLI / SDK login)

The Kagura Memory Python SDK (and any future first-party CLI) uses the
pre-registered `kagura-cli` public OAuth2 client to drive RFC 8628
device authorization grant — no client registration step is required
from the end user. From the terminal, `kagura auth login` does roughly:

1. SDK calls `POST /api/v1/oauth/device/authorize` with
   `client_id=kagura-cli`.
2. The server returns a `verification_uri` plus a short `user_code`.
3. The user opens `verification_uri` in a browser, signs in to Kagura
   Memory, picks the workspace they want to grant access to, and
   approves the consent screen.
4. The SDK polls `POST /api/v1/oauth/token` (with
   `grant_type=urn:ietf:params:oauth:grant-type:device_code`) and
   receives an `access_token` plus a `refresh_token` scoped to the
   chosen (user × workspace). An optional `resource` on the polling
   request must name this server's MCP resource (the rule above;
   `invalid_target` otherwise, checked before the authorization state)
   and becomes the token's audience. The device code is deleted when the
   token is issued: a later poll is refused, and its `user_code` no longer
   resolves on the `/device` page or through `POST /api/v1/oauth/device/verify`
   (`404`).
5. `kagura auth refresh` exchanges the refresh token for a new pair
   (refresh-token rotation is enforced server-side per RFC 6819
   §5.2.2.3 — the old access/refresh pair is revoked when a new pair
   is issued).
6. `kagura auth logout` calls `POST /api/v1/oauth/revoke` to revoke
   the issued tokens.

**Device authorization request.** `POST /api/v1/oauth/device/authorize`
takes `client_id` (required) and `scope` (optional, space-separated) in
either body encoding. The scope is granted by the rule of the authorization
code grant above:

```bash
# RFC 8628 §3.1 form encoding
curl -X POST http://localhost:8080/api/v1/oauth/device/authorize \
  -d client_id=kagura-cli -d scope=memory:read

# JSON
curl -X POST http://localhost:8080/api/v1/oauth/device/authorize \
  -H "Content-Type: application/json" \
  -d '{"client_id": "kagura-cli", "scope": "memory:read"}'
```

The media type is matched case-insensitively and parameters such as
`charset=utf-8` are ignored. In the form encoding, a parameter sent without a
value is treated as omitted and unrecognised parameters are ignored (RFC 6749
§3.1); a `%` that does not start a two-hex-digit escape makes the body
malformed. Errors use the RFC 6749 §5.2 body
`{"error": "...", "error_description": "..."}` with `Cache-Control: no-store`:

| Condition | Status | `error` |
|---|---|---|
| Unknown `client_id` | `400` | `invalid_client` |
| An admin-managed client registered without a `memory:*` scope | `400` | `invalid_scope` |
| Missing `client_id`, malformed body, `client_id` or `scope` sent twice | `400` | `invalid_request` |
| No `Content-Type`, or any other one (for example `text/plain` or `multipart/form-data`) | `400` | `invalid_request` |
| Body larger than 4096 bytes | `413` | `invalid_request` |
| The device authorization could not be stored | `500` | `server_error` |

The request limit below is counted before the body is read, so a refused
request counts against it too.

**Request limits.** The two unauthenticated device-flow steps are limited
per client address, counted over a one-minute window:

| Endpoint | Default limit | Setting | Over the limit |
|---|---|---|---|
| `POST /api/v1/oauth/device/authorize` (step 1) | 10 / min | `OAUTH_DEVICE_AUTHORIZE_RATE_LIMIT_PER_MINUTE` | `429` with the RFC 6749 §5.2 body `{"error": "invalid_request", "error_description": "..."}`, `Cache-Control: no-store` and `Retry-After: 60`. No device code is created. |
| `POST /api/v1/oauth/device/verify` (the `/device` page looking up the `user_code` in step 3) | 30 / min | `OAUTH_DEVICE_VERIFY_RATE_LIMIT_PER_MINUTE` | `429` with the standard error envelope (`message` set) and `Retry-After: 60`. Every lookup counts, whether the code exists or not. |

A client that gets a `429` should wait for `Retry-After` seconds before
retrying. If the rate-limit store (Redis) is unavailable, requests are let
through. Token polling (step 4) is not part of these limits; it is bounded
per device code by the `interval` / `slow_down` rules of RFC 8628 §3.5.

The limits key on the address the API server sees as the client
(`request.client.host`). Behind a reverse proxy this is the caller's address
only when uvicorn trusts the proxy's forwarded headers
(`--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`); otherwise all callers
share the proxy's budget.

Expired device codes are deleted by an hourly background job once they are
older than `OAUTH_DEVICE_CODE_RETENTION_SECONDS` past expiry (default
`3600`).

The `kagura-cli` row is seeded by alembic migration
`e10_624_seed_kagura_cli_client` and has these capabilities:

| Field | Value |
|---|---|
| `client_id` | `kagura-cli` |
| `client_name` | `Kagura Memory CLI` (shown on the `/device` consent page) |
| `token_endpoint_auth_method` | `none` (public client — no secret) |
| `grant_types` | `urn:ietf:params:oauth:grant-type:device_code`, `refresh_token` only |
| `scope` | `memory:read memory:write` |
| `redirect_uris` | `urn:ietf:wg:oauth:2.0:oob` (OOB sentinel for device-flow), `http://127.0.0.1:0/` (loopback wildcard reserved for future PKCE fallback) |

`memory:admin` is **intentionally excluded** from this client's scope
(narrowing-first ordering per #608 D1). Admin operations on memories
require a workspace-admin-managed client with an explicit non-default
scope grant — they are not reachable through the SDK device-flow login.

Workspace context is resolved at consent time from the signed-in
user's session, not from the client record (`owner_id=NULL`,
`workspace_id=NULL` on the seed row — same DCR pattern as #519).
Workspaces the user is not a member of are not selectable on the
`/device` consent screen.

SDK companion: `kagura-ai/kagura-memory-python-sdk#100`.

---

### Authorized apps and their default context

An OAuth grant can carry an MCP default context: a context-scoped tool called over a bearer token of that client without `context_id` runs in it (`?context=` on the MCP URL still wins). The consent page offers the choice when the app is authorized; these routes change it afterwards ([#2129](https://github.com/kagura-ai/memory-cloud/issues/2129)). Session auth only (like API key management: an API key or another app's token cannot change an app's default); only the caller's own grants are visible, and a `client_id` the caller holds no grant for is `404` like one that does not exist.

#### GET /api/v1/me/oauth/grants

The OAuth clients the caller holds a live token for (an access token or a refresh token not revoked), plus any with a stored setting, newest issuance first.

```json
[
  {
    "client_id": "abc123",
    "client_name": "Desktop App",
    "scopes": ["memory:read", "memory:write"],
    "default_context_id": "b3abeabe-7ab1-44bd-8e52-18a191bda66b",
    "default_context_name": "my-project",
    "last_issued_at": "2026-10-20T09:00:00Z"
  }
]
```

`default_context_name` is `null` when no default is set or the caller can no longer read the context; `scopes` is `[]` and `last_issued_at` is `null` for a grant kept only by its setting.

#### PUT /api/v1/me/oauth/grants/{client_id}/default-context

```json
{ "default_context_id": "b3abeabe-7ab1-44bd-8e52-18a191bda66b" }
```

Sets the grant's default (`null` clears it) and returns the grant in the shape above. The context must be readable by the caller now (viewer); otherwise `404` `Context not found`.

#### DELETE /api/v1/me/oauth/grants/{client_id}/default-context

Clears the grant's default. `204 No Content`.

---

## API Keys Management

### POST /api/v1/config/api-keys

Create a new API key (Admin only).

**Request Body:**

```json
{
  "name": "Production API Key",
  "scopes": ["memory:read", "memory:write"],
  "expires_at": "2026-11-22T00:00:00Z",
  "default_context_id": "b3abeabe-7ab1-44bd-8e52-18a191bda66b"
}
```

`default_context_id` (optional, [#2129](https://github.com/kagura-ai/memory-cloud/issues/2129)) is the key's MCP default context — see [PATCH](#patch-apiv1configapi-keyskey_id). A context you cannot read is `404` `Context not found`, and no key is created.

**Response:**

```json
{
  "id": "akey_3fJ0kQ9pLm2ZtX7cVb1NaR",
  "name": "Production API Key",
  "key": "kagura_abc123xyz789",
  "scopes": ["memory:read", "memory:write"],
  "created_at": "2025-11-22T10:00:00Z",
  "expires_at": "2026-11-22T00:00:00Z"
}
```

⚠️ **Important**: The `key` value is shown only once. Store it securely.

`name` is 1–100 characters. Names starting with `connector:` are reserved for
the write keys the server mints for ai-worker connectors and are refused with
`422` ([#1944](https://github.com/kagura-ai/memory-cloud/issues/1944)); the
same applies to every key created through the API (owner-provisioned member
keys and public-bound keys included).

---

### GET /api/v1/config/api-keys

List all API keys (Admin only).

**Response:**

```json
{
  "keys": [
    {
      "id": "akey_3fJ0kQ9pLm2ZtX7cVb1NaR",
      "name": "Production API Key",
      "scopes": ["memory:read", "memory:write"],
      "created_at": "2025-11-22T10:00:00Z",
      "expires_at": "2026-11-22T00:00:00Z",
      "last_used_at": "2025-11-22T15:30:00Z",
      "revoked_at": null
    }
  ]
}
```

---

### PATCH /api/v1/config/api-keys/{key_id}

Set or clear the key's MCP default context ([#2129](https://github.com/kagura-ai/memory-cloud/issues/2129)) — the only field this route changes. A context-scoped MCP tool called with this key and without `context_id` runs in it; `?context=` on the MCP URL still wins.

```json
{ "default_context_id": "b3abeabe-7ab1-44bd-8e52-18a191bda66b" }
```

`null` clears it. The context must be readable by the key's owner now, within the key's workspace scope; otherwise `404` `Context not found`. A public-bound or agent-bound key cannot carry one (`422`; an agent key uses its agent's default binding), and a revoked key is `400`. Returns the key in the list shape. The create, list and regenerate responses carry `default_context_id` and `default_context_name` (`null` when none is set or the owner can no longer read the context); regenerating a key keeps its default. A context in the trash stays the default (and is not readable) until it is restored or purged; purging clears it. A connector's shared key cannot carry one either (`422`), and it cannot be regenerated here or through the member credentials either (`422`, [#1952](https://github.com/kagura-ai/memory-cloud/issues/1952)): its connector holds the secret, so rotate it with `POST /api/v1/workspace-connectors/{connector_id}/rotate-kmc-key`.

---

### DELETE /api/v1/config/api-keys/{key_id}

Permanently delete an API key (Admin only).

`key_id` is the key's public id from the list or create response: `akey_` followed by 22 letters and digits. The integer database id is not accepted (an integer path id returns `422`), and a key that is not yours returns the same `404` as one that does not exist. The same applies to `/revoke`, `/regenerate` and `/stats`, to the share-key, resource-token and invitation routes (`skey_…`, `rtok_…`, `winv_…`), and to `DELETE /api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys/{key_id}`.

**Response:**

```json
{
  "message": "API key deleted successfully"
}
```

---

### POST /api/v1/config/api-keys/{key_id}/revoke

Revoke an API key (soft delete, preserves audit trail).

**Response:**

```json
{
  "message": "API key revoked successfully"
}
```

---

## External API Keys (BYOK)

Workspace-level provider credentials (OpenAI, Anthropic, Cohere, Voyage, …), stored encrypted and returned masked. Every route requires the workspace `Owner` role (Issue #381) and acts on the caller's current workspace.

| Endpoint | With `ENABLE_BYOK=false` | Purpose |
|---|---|---|
| `GET /api/v1/external-keys` | open | List the stored keys (`{"keys": [...], "total": n}`) |
| `POST /api/v1/external-keys` | `404` | Store a key (`key_name`, `provider`, `value`, optional `enabled`) |
| `PUT /api/v1/external-keys/{key_name}` | `404` | Replace a key's value |
| `PATCH /api/v1/external-keys/{key_name}/toggle` | open | Enable / disable a key (`{"enabled": bool}`) |
| `DELETE /api/v1/external-keys/{key_name}` | open | Remove a key |

The `404` on the two write routes is answered before authentication, so every caller sees the same response. List, toggle and delete stay open so an owner can always see and withdraw a credential that was stored before provisioning was turned off.

A key object:

```json
{
  "key_name": "OPENAI_API_KEY",
  "provider": "openai",
  "masked_value": "sk-p****9f2a",
  "user_id": "user_abc",
  "enabled": true,
  "is_protected": false,
  "created_at": "2026-01-10T09:00:00Z",
  "updated_at": "2026-01-10T09:00:00Z"
}
```

### Protected keys (`is_protected`, Issue #1613)

`is_protected` is `true` while the key can be neither deleted nor disabled; the web UI shows such a row as "Required". Only `OPENAI_API_KEY` can be protected, and only while something would break without it — all of:

- `ENABLE_BYOK=true` and `RESOLVE_STORED_BYOK_KEYS=true` (the defaults). With either off, no key is protected.
- OpenAI embeddings are in use: the deployment runs `EMBEDDING_PROVIDER=openai`, **or** the workspace has at least one live (not deleted) context whose embedding model is an OpenAI model.
  A legacy context that has no search-config row yet counts too: its next recall creates the row with the default OpenAI model, so the key would be needed right after it was deleted.

So on a deployment whose embeddings do not use OpenAI, a stored `OPENAI_API_KEY` is an ordinary key: deletable, and it can be disabled.

While a key is protected:

- `DELETE` answers `400`; the envelope's `message` is the reason, e.g. `{"error": "HTTP-400", "message": "Cannot delete OPENAI_API_KEY: OpenAI embeddings are in use by 2 contexts of this workspace.", "details": {}}`.
- `PATCH …/toggle` with `{"enabled": false}` (and `POST` with `"enabled": false`) answers `400` with a generic `message` (`Request failed`) and the structured reason under `details.detail`: `{"error": "cannot_disable_embeddings", "message": "Cannot disable OPENAI_API_KEY: OpenAI embeddings are in use by this deployment (EMBEDDING_PROVIDER=openai)."}`. Branch on `details.detail.error`, not on the top-level `error` (`HTTP-400`).
  This refusal alone also covers a key stored under another name whose `provider` is `openai` (possible through the API only): the embedding service selects the stored key by provider, so disabling it would break embeddings just the same. Such a key was never refused on `DELETE` and still is not, so it reports `is_protected: false`.

Status codes and body shapes are the same as before #1613; only the messages changed, and the refusals no longer fire when nothing reads the key. Re-enabling a disabled key is never refused by this rule.

---

## Beta Invite APIs

Closed-beta invite links (Issues #1581, #1595): a signed-in user mints a one-time link that lets one new person through the admin-configured signup gate. Off by default — every route below answers a plain `404` (before authentication) unless the deployment sets `ENABLE_BETA_INVITES=true`; `GET /api/v1/system/info` → `features.beta_invites` reports availability. Inviter routes accept a session cookie only (no API keys). See [Closed-beta invite links](deployment.md#closed-beta-invite-links-issue-1581) for the operator view.

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /api/v1/beta-invites/me` | session | The caller's quota standing and their invites, newest first |
| `POST /api/v1/beta-invites` | session | Mint a link (`201`), optionally labelled — the response carries the URL, once |
| `POST /api/v1/beta-invites/{id}/reissue` | session | Replace an own `active` / `expired` invite with a fresh link carrying the same label (`201`, same payload as mint) |
| `DELETE /api/v1/beta-invites/{id}` | session | Revoke an own, unused invite (`204`; idempotent) |
| `GET /api/v1/beta-invites/{token}/preview` | none | Landing-page check: is this link still usable? Read-only, per-IP rate-limited (30/min) |

`GET /api/v1/beta-invites/me`:

```json
{
  "quota": 4,
  "used": 2,
  "active": 1,
  "redeemed": 1,
  "remaining": 2,
  "invites": [
    {
      "id": "0b9f6c1e-5d0a-4a3e-9d57-2f4f3f6f8a11",
      "status": "active",
      "label": "Alice (university)",
      "created_at": "2026-09-01T12:00:00Z",
      "expires_at": "2026-09-08T12:00:00Z",
      "redeemed_at": null,
      "revoked_at": null,
      "redeemed_email": null
    },
    {
      "id": "5a0e2f0c-3c55-4f0e-8a55-0d6c1c0b7e42",
      "status": "redeemed",
      "label": null,
      "created_at": "2026-08-30T09:00:00Z",
      "expires_at": "2026-09-06T09:00:00Z",
      "redeemed_at": "2026-08-31T18:20:00Z",
      "revoked_at": null,
      "redeemed_email": "bob@example.com"
    }
  ]
}
```

- `status` is derived: `revoked` > `redeemed` > `expired` > `active`.
- `used` counts the invites occupying a quota slot; `active` (unused, still valid) and `redeemed` are its two parts — `active + redeemed == used` always. Expired and revoked invites free their slot.
- `quota` and `remaining` are `null` for a system admin (unlimited). Only in `SYSTEM_ADMIN_MODE=session`: in `off` / `service` an admin-role account is capped like everyone (#2767).
- `label` (since #1595) is the inviter's own note, or `null`. Only the inviter ever sees it.
- `redeemed_email` (since #1595) is the **current** e-mail of the account a `redeemed` invite admitted — the inviter vouched for that person, so they may see who it was. It is `null` for every other status, and `null` for a redeemed invite whose account no longer exists (erased) or whose allowlist entry an admin removed: it is read from the live account, never from a stored copy. #1581 exposed the lifecycle only; this field reverses that on purpose.

`POST /api/v1/beta-invites` → `201`. The JSON body is optional — sending no body at all (what clients written before #1595 do) mints an unlabelled invite:

```json
{ "label": "Alice (university)" }
```

`label` is free text for the inviter's own bookkeeping (a name, an address): surrounding whitespace is trimmed, an empty value means no label, and more than 100 characters, any control character (`U+0000`–`U+001F`, `U+007F`) or text that is not valid Unicode (a lone surrogate such as the JSON escape `"\ud800"`) is a `422` (`VAL-001`; the rejected value is never echoed). There is no endpoint to edit a label afterwards.

```json
{
  "id": "0b9f6c1e-5d0a-4a3e-9d57-2f4f3f6f8a11",
  "url": "https://your-domain.com/join/<token>",
  "expires_at": "2026-09-08T12:00:00Z",
  "label": "Alice (university)"
}
```

The link is valid for 7 days and works once. Only a hash of the token is stored, so the URL cannot be shown again — reissue the invite if the link is lost.

`POST /api/v1/beta-invites/{id}/reissue` → `201`, no request body, the same payload as mint. In one transaction it revokes `{id}` and mints a replacement with the same label; the old link stops working immediately. Allowed for the caller's own `active` and `expired` invites. Reissuing an `active` invite never changes the quota standing (the revoked link frees the slot the new one takes); an `expired` invite held no slot, so reissuing one needs a free slot like any mint. A second request for the same `{id}` — a double-click — is a `409` and mints nothing.

`GET /api/v1/beta-invites/{token}/preview` → `200 {"valid": true, "expires_at": "…"}`.

| Status | `error` | When |
|---|---|---|
| `404` | `HTTP-404` | Feature disabled (every route) |
| `404` | `RES-001` | Preview: unknown, revoked or malformed token · Revoke / reissue: unknown id or someone else's invite |
| `409` | `BETA-INVITE-001` | Mint, or reissue of an `expired` invite: the caller already holds `quota` invites (`details.reason` = `"quota_exceeded"`, `details.quota`). A refused reissue leaves the invite as it was |
| `409` | `BETA-INVITE-002` | Revoke / reissue: the invite was already used (`details.reason` = `"already_redeemed"`) |
| `409` | `BETA-INVITE-003` | Reissue: the invite was already revoked (`details.reason` = `"already_revoked"`). Revoke stays idempotent instead |
| `422` | `VAL-001` | Mint: `label` too long, contains a control character, is not a string, or is not valid Unicode |
| `410` | `RES-003` | Preview: expired or already redeemed |
| `429` | `RATE-001` | Preview: per-IP limit exceeded |

**Redeeming a link** is not an API call of its own: the landing page sends the invitee to `GET /api/v1/auth/{google|github}/login?return_to=…&invite=<token>`. The token's hash rides along with the OAuth state, and the signup gate spends the invite at the callback **only if it would otherwise have blocked the sign-in**. A malformed `invite` value is ignored rather than rejected.

---

## System APIs

### GET /health

Health check endpoint.

**Response:**

```json
{
  "status": "healthy",
  "timestamp": "2025-11-22T10:00:00Z"
}
```

---

### GET /api/v1/system/info

System information. Public (no authentication): the version plus non-sensitive deployment feature flags that the web UI reads before a workspace context exists. The `version` reflects the running server (current stable: see [GitHub Releases](https://github.com/kagura-ai/memory-cloud/releases)).

**Response:**

```json
{
  "name": "Kagura Memory Cloud",
  "version": "<server_version>",
  "description": "Remote MCP Server + Web Management",
  "environment": "production",
  "terms_version": null,
  "announcement_consent_version": null,
  "chatgpt_app_url": null,
  "search_defaults": {
    "use_rerank": false,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2",
    "content_chunks_enabled": false
  },
  "features": {
    "system_admin": true,
    "neural_memory": true,
    "research_tools": false,
    "plan_page": false,
    "byok": true,
    "cost_display": true,
    "managed_connectors": false,
    "managed_llm": false,
    "referrals": false,
    "beta_invites": false,
    "email_signup": false,
    "open_registration": false,
    "provider_link_confirmation": false,
    "public_mcp_domains": false,
    "reranking": true
  }
}
```

`features.email_signup` is `ENABLE_EMAIL_SIGNUP` and `features.open_registration` is `ALLOW_REGISTRATION` as configured (it is ignored while the signup gate is on) — see [Email + password sign-up](#email--password-sign-up).

`features.provider_link_confirmation` is `ENABLE_PROVIDER_LINK_CONFIRMATION` while a mail backend other than `logging` is configured: when true, a sign-in that ends at `email_in_use` may have mailed a confirmation link to an email sign-up account — see [Provider link confirmation](#provider-link-confirmation). It is per deployment and says nothing about any address.

`features.public_mcp_domains` is `MCP_PUBLIC_DOMAINS_ENABLED`: when true, owners may bind verified custom domains to their public MCP endpoints ([#2809](https://github.com/kagura-ai/memory-cloud/issues/2809), [Custom domains of a public MCP endpoint](#custom-domains-of-a-public-mcp-endpoint)). It says nothing about the deployment's hosts.

`search_defaults` are the reranker values and the content chunk flag (`DEFAULT_CONTENT_CHUNKS_ENABLED`) new contexts are created with (provider and model names only). Each `features` flag mirrors a deployment setting (`ENABLE_*`, `MANAGED_LLM_PROVIDER`); the values above are illustrative, not the defaults of every deployment. `terms_version` is the deployment's `TERMS_VERSION`, or `null` when it is empty (server-side terms acceptance off) — see [Terms-of-service acceptance](#terms-of-service-acceptance). `announcement_consent_version` is the version of the announcement-email consent wording while `ANNOUNCEMENT_EMAILS_ENABLED` is on, `null` otherwise — see [Announcement-email consent](#announcement-email-consent). `chatgpt_app_url` is the deployment's `CHATGPT_APP_URL` (the public URL of its app in ChatGPT's app directory), or `null` when it is empty — see [ChatGPT app link](deployment.md#chatgpt-app-link-issue-2760). `features.system_admin` is `true` only when this process serves the system-admin surface to signed-in admins (`SYSTEM_ADMIN_MODE=session`, the default); the web UI hides the admin menu and `/admin/*` otherwise (#2767, [Admin APIs](#admin-apis)).

---

## Terms-of-service acceptance

Issue #1665. Everything below is inert while the deployment's `TERMS_VERSION` is empty; see [Terms-of-service acceptance](deployment.md#terms-of-service-acceptance-issue-1665) for the operator view.

- `GET /api/v1/auth/{google,github}/login?accepted_terms=<version>` — the version the person agreed to, bound to the OAuth state. A sign-up (an identity with no account yet) without the current version is refused: no account is created and the callback redirects to `{FRONTEND_URL}/login?error=terms_required&provider=<provider>` (plus the flow's `return_to` when it is safe). A browser flow that also carries a well-formed `invite=` (with `ENABLE_BETA_INVITES` on) is redirected by the login endpoint itself, before the provider, to `{FRONTEND_URL}/join/{token}?error=terms_required` (plus a safe `return_to`). Existing users — including an identity whose e-mail already belongs to an account, which still ends on `error=email_in_use` — are not refused.
- `POST /api/v1/auth/login` accepts an optional `"accepted_terms": "<version>"` in the body; it is recorded on success (after `POST /api/v1/auth/mfa/verify` when MFA is on).
- `GET /api/v1/auth/me` → `user.terms_acceptance_required`: `true` when a version is configured and the user's latest accepted version differs; `user.terms_version`: the current version (`null` when off). An acceptance from an OAuth sign-in is recorded for the account that owns the identity — for a provider linked to another account, that account.

### POST /api/v1/me/terms-acceptance

Session cookie only. Records that the signed-in user accepts the current terms version (source `reaccept`) and writes a `terms.accepted` audit row naming the version.

**Request:** `{"version": "2026-09"}`

**Response (200):**

```json
{ "version": "2026-09", "recorded": true, "terms_acceptance_required": false }
```

`recorded` is `false` when that version was already the user's latest acceptance (a repeat is still `200`).

| Status | When |
|---|---|
| `404` | `TERMS_VERSION` is empty — the feature is off |
| `409` | `version` is not the current terms version (the page loaded before it changed); `detail` says to reload |
| `401` / `403` | no session / an API key or bearer token instead of a session |

---

## Announcement-email consent

Issue #2761. Whether the person wants announcement emails (new features, product news). Service notices (invitations, password, security, capacity, erasure) never depend on it. See [Announcement email consent](deployment.md#announcement-email-consent-issue-2761) for the operator view.

- `GET /api/v1/auth/{google,github}/login?announcement_emails=<true|false>&announcement_consent_version=<version>` — the answer to the unchecked-by-default sign-up checkbox, bound to the OAuth state. Recorded (source `signup`) only when the sign-in creates the account and only with the current version from `/system/info`; ignored while `ANNOUNCEMENT_EMAILS_ENABLED` is off, for any other value, and when the request's `Sec-Fetch-Site` is `cross-site` or `none`; a `true` answer is recorded only when the callback runs in the browser that called this URL (its sign-in nonce cookie). It never blocks a sign-in.

### GET /api/v1/me/announcement-emails

Session cookie only.

**Response (200):**

```json
{
  "offered": true,
  "announcement_emails": false,
  "answered": true,
  "source": "signup",
  "recorded_at": "2026-11-12T09:00:00Z",
  "wording_version": "2026-11"
}
```

`offered` is `false` while `ANNOUNCEMENT_EMAILS_ENABLED` is off. `answered` is `false` (and `source` / `recorded_at` `null`) when the user never answered. `wording_version` is the current consent wording to send back when turning the emails on (`null` while not offered).

### PUT /api/v1/me/announcement-emails

Session cookie only. Records a new answer with source `settings`; an unchanged answer writes nothing.

**Request:** `{"announcement_emails": true, "wording_version": "2026-11"}` (`wording_version` is needed only to turn them on)

**Response (200):** the body of `GET` plus `"recorded": true|false`.

| Status | When |
|---|---|
| `409` | turning them on while `ANNOUNCEMENT_EMAILS_ENABLED` is off, or with a `wording_version` other than the current one (the page loaded before the wording changed); turning them off always works |
| `429` | more than 20 `PUT` requests that turn them on in an hour (an unchanged answer counts too); a request that turns them off is not counted and never refused |
| `401` / `403` | no session / an API key or bearer token instead of a session |

### POST /api/v1/announcement-emails/unsubscribe/{token}

No authentication: the token from an announcement email is the only authority. The one-click target named in the `List-Unsubscribe` header (RFC 8058); the body is not read. Turns announcement emails off for the token's account (source `unsubscribe_link`) and can do nothing else. Always `200 {"status": "accepted"}` — for a valid token, a forged or malformed one, an account that no longer exists, and an answer that was already off — and never `429`. `503` only when a valid token could not be applied (try again).

### GET /api/v1/announcement-emails/unsubscribe/{token}

No authentication. Changes nothing: `303` to `{FRONTEND_URL}/email/unsubscribe`, with `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. For a valid token the fragment (`#<token>`) holds a token the server issues afresh for the same account; the path's own text never goes into `Location`, and any other value lands on the page without a fragment. Checking the token reads no database.

### POST /api/v1/announcement-emails/unsubscribe

No authentication. What the unsubscribe page posts: `{"token": "<token>"}`. Same behaviour and answer as the one-click route, except that invalid tokens are counted per client address and answered `429` past 30 in 15 minutes. A valid token is never refused.

---

## Service notices over MCP

Issue #2762. Whether the person wants short service notices from this deployment's operator (a maintenance window, a change that needs action) in the MCP `bootstrap` reply of their AI tools. Off by default; kept in its own history, apart from the announcement-email answers. See [Service notices over MCP](deployment.md#service-notices-over-mcp-issue-2762) for the operator view and [`bootstrap` → `notices`](mcp-tools.md#service-notices--notices) for what the tools receive.

### GET /api/v1/me/mcp-notices

Session cookie only.

**Response (200):**

```json
{
  "offered": true,
  "mcp_notices": false,
  "answered": false,
  "source": null,
  "recorded_at": null,
  "wording_version": "2026-11"
}
```

`offered` is `false` while `MCP_NOTICES_ENABLED` is off. `answered` is `false` (and `source` / `recorded_at` `null`) when the user never answered; `source` is otherwise `settings`. `wording_version` is the current consent wording to send back when turning notices on (`null` while not offered).

### PUT /api/v1/me/mcp-notices

Session cookie only. Records a new answer with source `settings`; an unchanged answer writes nothing.

**Request:** `{"mcp_notices": true, "wording_version": "2026-11"}` (`wording_version` is needed only to turn them on; no other key is accepted)

**Response (200):** the body of `GET` plus `"recorded": true|false`.

| Status | When |
|---|---|
| `409 MCPNOTICE-005` | turning them on while `MCP_NOTICES_ENABLED` is off; turning them off always works |
| `409 MCPNOTICE-006` | turning them on with a `wording_version` other than the current one (the page loaded before the wording changed) |
| `429` | more than 20 `PUT` requests that turn them on in an hour (an unchanged answer counts too); a request that turns them off is not counted and never refused |
| `401` / `403` | no session / an API key or bearer token instead of a session |

---

## Memory Health

The per-context memory health report (#1211, #1225), for the contexts the
caller created. Session only: an API key or an OAuth bearer token gets `403`.
Any signed-in user may call it; no system-admin or workspace role is needed,
because every number in it is the caller's own (#2764).

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/memory-health` | Breakdown: one graded entry per context the caller created (consolidation / graph / retrieval statuses, `ok`/`warn`/`fail`) from Sleep telemetry, graph invariants and usage stats, plus an entry with `context_id: null` when signals were recorded outside those contexts. |
| `GET /api/v1/memory-health?context_id=<uuid>` | The 3-section detail of one context the caller created. A context created by someone else and an unknown id get the same `404`. |
| `GET /api/v1/memory-health?context_id=unattributed` | The 3-section detail of the signals recorded outside the caller's contexts. |

| Status | When |
|--------|------|
| `401` / `403` | no session / an API key or bearer token instead of a session |
| `404` | the context does not exist or the caller did not create it |
| `422` | `context_id` is neither a UUID nor `unattributed` |

The web UI shows the detail on the **Health** tab of a context the viewer
created and a status badge on each of those contexts in the context list.
See [Memory Health Report](ops/memory-health-report.md) for every metric and
threshold.

---

## Admin APIs

Admin endpoints require `system_admin` or `workspace_admin` role.

The system-admin routes (`/api/v1/admin/*`, `PUT /api/v1/config/{key}`, `POST /api/v1/config/batch`, `GET /api/v1/oauth/providers`) follow the deployment's `SYSTEM_ADMIN_MODE` (#2767; [System-admin surface](deployment.md#system-admin-surface-issue-2767)):

- `session` (default): a session cookie of a user with `role=admin`; `401` without a session, `403` (`HTTP-403`) otherwise. Unchanged.
- `off`: every such route answers `404` with the unknown-path body (`HTTP-404`), whatever the caller sends, and is left out of the OpenAPI document.
- `service`: only an operator console. Send `Authorization: Bearer <SYSTEM_ADMIN_SERVICE_TOKEN>` and `X-Operator-Email: <address of the person acting>`; a session cookie is ignored. Errors, in this order: `404` outside `SYSTEM_ADMIN_SERVICE_ALLOWED_CIDRS`, `503 SYSADMIN-001` (credential not configured), `401 SYSADMIN-002` (credential missing, another scheme, wrong, or sent in two `Authorization` headers even when the first one is valid; `WWW-Authenticate: Bearer`), `400 SYSADMIN-003` (`X-Operator-Email` missing, repeated or not a plain address), `403 SYSADMIN-004` (a route that acts on the caller's own account: `POST /admin/sleep/run`, `POST /admin/sleep/actions/{action_id}/undo-merge`, `GET /admin/memory-health`, and `POST /admin/contexts/recover` with `dry_run=false`; for Sleep an operator calls the target-user routes of #2838 instead, see [Sleep Maintenance](#sleep-maintenance)). Writes record the actor as `operator:<address>`.

`PUT /api/v1/admin/users/{user_id}/role` writes a `system_role_update` audit row with the acting admin or operator (#2767). Its `role` is `admin` or `user`, the two system roles; any other name, `read_only` included, answers `422` and changes nothing (#2854).

Two rules protect the accounts with `role=admin`, in every mode and on both routes that can take the role away (`PUT /api/v1/admin/users/{user_id}/role` with another role, and `DELETE /api/v1/admin/system-admins/{user_id}`) (#2828): the initial system administrator keeps the role, and so does the last remaining admin. Either answers `403 ADMIN-001` with the message naming the rule, changes nothing and writes no audit row. The check and the change are one transaction under a lock on the admin rows, so two demotions sent at once leave one admin: one succeeds, the other answers `403 ADMIN-001`. Promotion to `admin` and changes that take the admin role from nobody are not affected. On the role PUT a session admin still cannot change their own admin role; the demote route has no such check. On both routes a `user_id` that holds a NUL character answers `422`.

The deletion of an admin account (`DELETE /api/v1/admin/users/{user_id}` and `POST /api/v1/admin/users/{user_id}/erase`) checks the same two rules under the same lock, in the transaction that deletes the account (#2855): a deletion and a demotion, or two deletions, sent at once leave one admin, and the refused request answers as before (`403 ADMIN-001` for the delete, `403 ERASURE-004` / `403 ERASURE-003` for the erasure) and changes nothing. An admin erasure decides in the transaction that creates its request, and its run holds no admin row: an account with an open erasure request (`pending`, `cooling_off` or `in_progress`, self-service or admin-started) does not count as a remaining admin for any role change, deletion or erasure, and counts again once its request ends `failed` or `cancelled`. On both routes a `user_id` that holds a NUL character answers `422`, and so does a `reason_detail` with one on the erasure, or a successor id with one in `workspace_transfers` / `context_transfers` on the delete (#2857).

### Users

How each account was admitted (Issue #2803). Both routes answer a session admin in `session` and a service operator in `service`, and `404` in `off`, like every system-admin route.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/users` | The user list. Each row carries `admitted_via`. Two filters, combined with the existing `search`, `role`, `workspace_id` and `plan`: `admitted_via=<value>` (one of the values below; anything else is a `422`) and `invited_by=<user_id>` (1-255 characters: the accounts admitted by an invite that user minted). `total` counts the users that match every filter, so it gives the number of accounts per channel and per inviter. `user_id=<user_id>` (1-255 characters, no NUL character, #2843) finds one account by its own id: the match is exact and case-sensitive, nothing is trimmed, and it is ANDed with the other filters. The answer is the same list shape with at most one row and `total` 0 or 1; an id that is no account answers `200` with `{"users": [], "total": 0}`, not `404`. It reads the row, its memory count and its owned-workspace cap only (plus the live workspaces with `include_workspaces=true`), so it is the light way to put an address and a name beside a bare user id. Bounds of the other parameters: `workspace_id` is a UUID (an empty value is no filter), `offset` goes from 0 to 2^63 - 1, and `search`, `role`, `plan` and `invited_by` hold no NUL character; a value outside these answers `422`. |
| `GET /api/v1/admin/users/{user_id}` | The user detail. `admission` = `{"via", "invite_issuer", "invite_id", "invited_by"}`. |

`admitted_via` is written once, by the code that creates the account, and never rewritten:

| Value | Admitted by |
|-------|-------------|
| `beta_invite` | a closed-beta invite link ([Closed-beta invite links](deployment.md#closed-beta-invite-links-issue-1581)) |
| `allowlist` | a manual allowlist row (a pending Google row included) |
| `workspace_invitation` | a pending workspace invitation addressed to the verified address |
| `open_registration` | `ALLOW_REGISTRATION` with the signup gate off |
| `first_user` | the first account of an empty deployment |
| `admin_cli` | a local admin created by `create_admin` / `seed_e2e_admin` |
| `unknown` | no evidence: accounts created before v0.162.0 that the backfill could not place, and a second account that lost the first-user race |

For `beta_invite`:

- `invite_id` is the id of the invite spent (it stays after the invite row is gone);
- `invite_issuer` is `admin` when the inviter's `users.role` was `admin` when the account was created, in every `SYSTEM_ADMIN_MODE`, and `user` otherwise; it is null when the inviter's account was already gone then;
- `invited_by` (`{"user_id", "email"}`) names the inviter of a user-minted invite. It is read live from the inviter's account: null for an admin-minted invite and once the inviter's account is erased or deleted.
- accounts created before v0.162.0 were filled by the upgrade's backfill ([How each account was admitted](deployment.md#how-each-account-was-admitted-issue-2803)): their `invite_issuer` is the inviter's role at upgrade time, not at sign-up, and `invite_id`, `invite_issuer` and `invited_by` are all null when no live invite remained then, even though `via` is `beta_invite`.

For every other value the three fields are null. The invite's label (the inviter's private note) is never part of a response, a log line or an audit row. Neither filter value is written to the application's structured log lines or audit rows; the request line itself (the API's uvicorn access log and a reverse proxy's access log) records the query string, as for any admin URL that names a user.

### Workspace plans

The plan and add-on reads under `/api/v1/admin/plans`. They answer a session admin in `session` and a service operator in `service`, and `404` in `off`, like every system-admin route.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/plans/workspaces` | The live workspaces with their plan, one page at a time (Issue #2841): `{"items", "total", "limit", "offset"}`. Each item is `{"id", "name", "plan_name", "owner_user_id", "owner_name", "owner_email", "total_memories", "memory_limit", "mcp_calls_per_day", "mcp_calls_per_week", "entitlement_source"}`. |
| `GET /api/v1/admin/plans/workspaces/{workspace_id}` | One live workspace, as the list's item for it (Issue #2841). `404` (`RES-001`, "Workspace not found") for an unknown or soft-deleted workspace; `422` for an id that is not a UUID. |
| `GET /api/v1/admin/plans/workspaces/{workspace_id}/quotas` | One workspace's limits: `base`, `addon`, `effective`, `usage`, `spend_cap`, its `entitlement_source` and its `addon_records`. `404` for an unknown or soft-deleted workspace; `422` for an id that is not a UUID. |
| `GET /api/v1/admin/plans/audit` | The plan changes made through the admin plan route, newest first, as a bare array of `{"id", "workspace_id", "workspace_name", "old_plan", "new_plan", "changed_by", "changed_by_name", "changed_by_email", "changed_at", "reason"}`. Parameters (Issue #2830): `workspace_id` (a UUID: only that workspace's changes, a soft-deleted workspace's included; an id with no change answers `[]`), `limit` (1 to 500, default 100) and `offset` (from 0, at most 2^63 - 1). The order is `changed_at` descending, then `id` descending; page until a page holds fewer entries than `limit`. A value outside these rules answers `422` (`VAL-001`). |

The workspace list's parameters, all optional and combined with AND:

| Parameter | Meaning |
|-----------|---------|
| `limit` | Page size, 1 to 500 (default 50). |
| `offset` | Rows to skip, from 0 (default 0), at most 2^63 - 1. |
| `search` | A case-insensitive substring of the workspace name, at most 255 characters. It is trimmed and an empty value means no filter; `\`, `%` and `_` match literally. |
| `plan` | Exact: one of the plan keys (`free`, `basic`, `pro`, `promax`). |
| `owner_user_id` | Exact, 1 to 255 characters without a NUL. |

A value outside these rules (a `limit` of 0 or 501, a negative `offset`, an unknown `plan`, a `search` that is too long or holds a control character) answers `422` (`VAL-001`); nothing is clamped. The order is `created_at` ascending, then `id`, so walking the pages with `offset` returns each workspace once while none is added or removed. `total` counts every live workspace that matches the filters; soft-deleted workspaces are in neither `items` nor `total`. The `search` text is not written to the application's structured log lines. Until v0.164.0 the route took no parameter and answered every live workspace as a bare array.

`entitlement_source` (Issue #2829) says who owns the workspace's plan: `admin_grant` (the default, and what `PUT /api/v1/admin/plans/workspaces/{workspace_id}/plan` sets) or `external_billing` (what a billing push sets). Only an `external_billing` workspace counts as under a running contract or can be capacity-locked.

`addon_records` (Issue #2829) are the workspace's active add-on records, the rows its add-on limits are computed from: `{"id", "addon_type", "source", "quantity", "bonus", "active_from", "active_until", "created_at", "created_by"}`, ordered by `addon_type`, then `source` (`admin_grant` or `stripe`). `bonus` is `quantity` times the unit value of the add-on type, so the sum of `bonus` per add-on type is the `expected_value` of `GET /api/v1/admin/plans/addon-cache-consistency`. `active_until` is `null` for a record with no end; a record that has expired or starts later is not listed. A later admin grant changes only `quantity`, so `created_by` names who first granted it. A billing push that carries add-ons sets the workspace's add-on values directly and writes no record.

### Signup allowlist

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/signup-gate/allowlist` | The signup allowlist, one page at a time (Issue #2831): `{"items", "total", "limit", "offset"}`, newest first (`created_at` descending, then `id`). Each item is `{"id", "provider", "subject_id", "subject_label", "github_user_id", "github_username", "source", "state", "added_by_user_id", "created_at"}`. |

Its parameters, all optional and combined with AND:

| Parameter | Meaning |
|-----------|---------|
| `limit` | Page size, 1 to 200 (default 50). |
| `offset` | Rows to skip, from 0 (default 0), at most 2^63 - 1. |
| `search` | A case-insensitive substring of `subject_label`, at most 255 characters. It is trimmed and an empty value means no filter; `\`, `%` and `_` match literally. `subject_id` and the legacy `github_*` fields are not searched. |
| `provider` | Exact: `github`, `google` or `email`. |
| `source` | Exact: `manual`, `github_sponsors` or `beta_invite`. |
| `state` | Exact: `active`, `grace` or `revoked`. |

A value outside these rules answers `422` (`VAL-001`); nothing is clamped. `total` counts every row that matches the filters. Walking the pages with `offset` returns each row once while none is added; a row added meanwhile (an admin add, a redeemed closed-beta invite link) is the newest and moves the later rows down by one. Until v0.164.0 the route took no parameter and answered every row as a bare array.

### Referral ledger

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/referrals` | The referral grants, newest first: `{"items", "total", "limit", "offset"}`. Filters, combined with AND: `referrer_user_id` (the grants a user earned by inviting others), `referred_user_id` (Issue #2836: the grant a user received; exact, and a user is referred at most once, so at most one row) and `include_revoked` (default `true`). `limit` 1 to 200 (default 50), `offset` from 0 (at most 2^63 - 1). A user id with a NUL, like any value outside these, answers `422`. The grant of an erased user is not found by `referred_user_id`. |

### Deleted contexts

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/contexts/deleted` | The soft-deleted contexts a restore can be asked for (#2833): the contexts with a deletion time whose workspace is not deleted. Read only, no audit row. A session admin in `session`, a service operator in `service`, `404` in `off`. |

Query: `workspace_id` (UUID), `created_by` and `deleted_by` (1-255 characters without a NUL character, exact match on the stored value; `deleted_by` is a user id or `system:capacity-purge`), ANDed; `limit` 1-500 (default 50) and `offset` from 0 to 2^63-1. A value outside these answers `422`. Order: `deleted_at` newest first, then `id`.

Answer `{"items": [...], "total", "limit", "offset"}`; `total` counts the filtered rows. An item:

| Field | Value |
|-------|-------|
| `context_id`, `name` | the context |
| `workspace_id`, `workspace_name` | its workspace |
| `created_by`, `deleted_by` | the stored ids, or null |
| `deleted_at` | UTC time of the deletion |
| `deleted_memories` | the context's soft-deleted memory rows still held: `memories_restored + memories_left_deleted` of the restore's dry run. `0` once the tombstone purge removed them: the context is still listed, and a restore brings it back empty |
| `name_taken` | a live context of the workspace has this name: `POST /api/v1/admin/contexts/{context_id}/restore` answers `409` without `new_name` |
| `resource_taken` | the context has a `resource_id` and a live context serves it: the restore answers `409` |

With both flags false the restore's dry run answers `200`. The answer holds no description, summary or usage guide of the context, no memory text and no email address; a context of a deleted workspace is not listed, because the restore always refuses it. See [Restoring a deleted context](deployment.md#restoring-a-deleted-context--issue-1804).

### Failed embeddings

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/embedding/failures` | Where failed embeddings are, per workspace and context, in counts (#2832). Read only, no audit row. A session admin in `session`, a service operator in `service`, `404` in `off`. |
| `POST /api/v1/admin/embedding/retry-failed` | Reset failed embeddings to `pending` for a `workspace_id`, a `context_id` or both (at least one; an empty value is no filter, and no filter is a `400`). Both are UUIDs: a value that is not one is a `422`, as on the read. Answers `reset_count`. |

The read counts the rows the retry counts (`embedding_status = 'failed'`, not deleted), grouped by `workspace_id` and `context_id`; memories without a context are a group with `context_id: null`. Query: optional `workspace_id` and `context_id` (UUIDs), `limit` 1-500 (default 100), `offset` from 0 to 2^63-1; a value outside these answers `422`. Order: `failed` descending, then `workspace_id`, then `context_id`. Answer `{"total", "limit", "offset", "groups": [...]}`; `total` is the number of groups. A group:

| Field | Value |
|-------|-------|
| `workspace_id`, `workspace_name` | the workspace; the name is null when it has no row |
| `context_id`, `context_name` | the context, both null for memories without one |
| `failed` | failed rows that are not an erased author's: what the retry resets without `include_erased` |
| `stalled` | the part of `failed` with no retry left (`embedding_retry_count >= 3`): nothing retries it on its own, only the retry route starts it again |
| `capped` | the part of `failed` refused by the embedding spend cap (`QUOTA-002`) |
| `configuration` | the part of `failed` that failed on configuration (`CFG-*`, `FEAT-*`: no credential, no plan feature, an unreachable self-hosted endpoint). The sweep retries these without bound, so a reset changes nothing until the configuration does; the same holds for `capped` and the cap |
| `other` | the rest of `failed` (a provider or input failure, and rows failed before the code prefix existed) |
| `erased` | failed rows of an erased author, kept out of the queue. `failed + erased` is what the retry resets with `include_erased=true` |
| `oldest_attempt_at`, `newest_attempt_at` | the earliest and latest embedding attempt among the group's rows, null when none has one |

`failed = capped + configuration + other`. The answer holds no memory id, summary, content, tag, author id or stored error text: the error is answered as a class only. The deployment-wide totals are in [Operations status](#operations-status) (`embedding_failed_retryable`, `embedding_failed_stalled`).

### Ownership force-transfer requests

With `REQUIRE_DUAL_CONTROL_FORCE_TRANSFER` on, `POST /api/v1/admin/workspaces/{workspace_id}/force-transfer-ownership` files a `pending` request (`202` with its `request_id`) that a second, different system admin approves or cancels.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/force-transfer-requests` | The requests, newest first, then by `id` (#2846), so the approver finds the ones that wait without being given an id. Read only, no audit row. A session admin in `session`, a service operator in `service`, `404` in `off`. |
| `GET /api/v1/admin/force-transfer-requests/{request_id}` | One request. |

Query of the list: `status` (`pending`, `approved`, `cancelled` or `superseded`) and `workspace_id` (UUID), both optional and ANDed; `limit` 1-500 (default 50) and `offset` from 0 to 2^63-1. A value outside these answers `422`. Answer `{"items": [...], "total", "limit", "offset"}`; `total` counts the filtered rows, and for `status=pending` it is the `force_transfer_requests_pending` count of [Operations status](#operations-status).

An item has the fields of the read by id and no other: `request_id`, `workspace_id`, `target_user_id`, `reason` (the initiating admin's text), `status`, `initiated_by_user_id`, `initiated_by_email` (as stored: `operator:<address>` in `service` mode; the four-eyes check compares it), `ownership_epoch_at_initiation`, `created_at`, `decided_by_user_id`, `decided_at`. The deciding admin's address is not answered, and no name or address is looked up for the workspace, its owner or the target user.

The list reads the request table alone. A request whose workspace was deleted is still listed, and stays `pending`: approving, cancelling and filing a new request all need the live workspace. Filing a request for a workspace marks its pending one `superseded`, so `?status=pending&workspace_id=<id>` shows first whether one is waiting.

### Operations status

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/operations/status` | The state of operations in aggregates only (#2768): every scheduled job's last run, every work queue's size and oldest item, every platform LLM lane's recorded usage and latest probe. Any process answers (stored rows only). A service operator gets `200`. `Cache-Control: no-store`; concurrent polls of one process share one read for 5 s. |

The response (`schema_version: 1`) has `as_of`, `overdue_grace_seconds`, `scheduler_registered_at`, `probe_enabled`, and three lists:

| Field | Entry fields |
|-------|--------------|
| `jobs` | `job_id`, `name`, `trigger` (`interval` / `cron` / `other`), `expected_interval_seconds`, `registered`, `running`, `overdue`, `last_started_at`, `last_finished_at`, `last_outcome` (`ok` / `failed` / `missed`), `last_outcome_at`, `last_success_at`, `consecutive_failures`, `error_class` (exception class name, never the message), `last_duration_ms`, `counts` (integer counts the run returned) |
| `backlogs` | `name`, `drained_by` (job ids; empty = people), `count`, `oldest_at`, `oldest_age_seconds`, `error_class` (set when the count could not be read; `DeadlineExceeded` when the request ran out of time) |
| `llm` | `lane` (`embedding`, `sleep_judge`, `supersede_verify`, `recall_selector`, `analysis`, `rerank`), `provider`, `model`, `credential_present`, `snapshot_at`, `usage_recorded`, `usage_attribution` (`platform` / `includes_workspace_keys`), `last_success_at`, `calls_last_hour`, `calls_last_day`, `error_class`, `probe` (`null` unless the opt-in probe ran: `last_probe_at`, `last_outcome`, `last_latency_ms`, `error_class`, `consecutive_failures`, `last_success_at`) |

No user, workspace or context identifier, email address, memory text or exception message is part of the response. See [Operations status](deployment.md#operations-status-scheduled-jobs-backlogs-llm-lanes--issue-2768) for the meaning of each field, the probe setting and what to alert on.

### Erasure requests

Issue #2834. Read-only: where an account erasure stands after it was requested. Both routes answer a session admin in `session` and a service operator in `service`, and `404` in `off`, like every system-admin route. Neither writes a row.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/erasure-requests` | A page of requests, newest `requested_at` first, then by `request_id`: `{"items", "total", "limit", "offset"}`. Filters, combined with AND: `status` (`pending`, `cooling_off`, `in_progress`, `complete`, `failed` or `cancelled`), `user_id` (1-255 characters without a NUL, compared exactly with the stored value) and `is_self_service` (`true` / `false`). `limit` 1-200 (default 50), `offset` from 0 (at most 2^63 - 1). A value outside these is a `422`. `total` counts the requests that match the filters. |
| `GET /api/v1/admin/erasure-requests/{request_id}` | One request, with the same fields. `404 RES-001` for an unknown id, `422` for an id that is not a UUID. |

A request:

| Field | Meaning |
|-------|---------|
| `request_id` | The request's id, as `POST /api/v1/admin/users/{user_id}/erase` answered it. |
| `user_id` | As stored: the subject's id until the erasure completes, the subject's pseudonym afterwards (also on a request that failed after its Postgres step). The `user_id` filter therefore finds a completed request by its pseudonym, not by the id the account had. |
| `status` | One of the six statuses above. |
| `is_self_service`, `reason_code` | Who asked (the member, or an admin on the member's behalf) and the reason code given. |
| `initiated_by` | As stored: the admin's user id, `operator:<address>` for a request made in `service` mode, or the subject for a self-service request (pseudonymized with it). |
| `requested_at`, `confirmed_at`, `scheduled_for`, `started_at`, `completed_at`, `cancelled_at` | Times with their zone; null when the step did not happen. `scheduled_for` is when the sweep runs a `cooling_off` request next. |
| `retry` | `{"attempts", "step", "error_class", "last_attempt_at", "embeddings_requeued"}` for a request that was rescheduled (`503 ERASURE-007`, a lock conflict) or whose embeddings were re-queued; null otherwise. A key the request does not have is null. |
| `failure_kind` | For a `failed` request, one of `after_postgres_commit` (the account is erased in Postgres; a later step failed or was interrupted), `billing_contract_active`, `workspace_transfer_required`, `vector_store_unavailable`, `lock_conflict` or `other`. Null for every other status. |

### Audit log

Issue #2835. Read-only: the `audit_logs` rows that admin actions and sign-up decisions write. The route answers a session admin in `session` and a service operator in `service`, and `404` in `off`, like every system-admin route. Reading writes no audit row.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/audit-logs` | The rows of the actions listed below, newest first (`id` descending): `{"entries": [...], "next_before_id": <id or null>}`. |

Query parameters, all optional and combined with AND. A value outside these rules is a `422`:

| Parameter | Meaning |
|-----------|---------|
| `action` | Repeatable. Each value must be one of the actions listed below. Without it the route returns every listed action. |
| `user_id`, `user_email`, `resource` | 1-255 characters without a NUL, compared exactly with the stored value (no trimming, no case folding, no wildcard). |
| `from`, `to` | Bounds on `created_at`: `from` inclusive, `to` exclusive. ISO 8601 with a zone (`2026-11-15T09:00:00Z`; percent-encode the `+` of an offset as `%2B`). A value without a zone, a time whose UTC value is outside the years 1 to 9999, and `from` later than `to`, are refused. |
| `limit` | 1 to 100 (default 50). |
| `before_id` | Cursor, 1 to 2147483647: rows with a smaller `id`. Pass the previous answer's `next_before_id`; it is `null` on the last page. |

An entry: `{"id", "created_at", "action", "user_id", "user_email", "resource", "old_value", "new_value", "metadata"}`. `created_at` is UTC with its zone. `user_id`, `user_email` and `resource` are answered as stored, so after an account erasure a row shows what the erasure left (pseudonyms). In `service` mode a writer stores the actor as `operator:<address>`. What the three columns mean depends on the action:

| `action` | `user_id` | `user_email` | `resource` | `metadata` keys |
|----------|-----------|--------------|------------|-----------------|
| `system_role_update` | the actor | the actor | `user:<target id>` | `actor_user_id`, `target_user_id`, `old_role`, `new_role` |
| `system_admin_promote`, `system_admin_demote` | the target | the actor | `user:<target address>` | `promoted_by`, `is_initial_admin` / `demoted_by`, `was_initial_admin` |
| `workspace_slot_bonus_update` | the actor | the actor | `user:<target address>` | `actor_user_id`, `target_user_id`, `before_value`, `after_value`, `delta`, `reason` |
| `referral_grant_revoke` | the actor | the actor | `referral_grant:<id>` | `actor_user_id`, `referrer_user_id`, `referred_user_id`, `referrer_bonus_memories`, `referred_bonus_memories`, `reason` |
| `workspace_ownership_force_transfer` | the actor | the actor | `workspace:<id>` | `break_glass`, `reason`, `previous_owner_id`, `new_owner_id`, `ownership_epoch`, `target_was_member`, `self_transfer`; with dual control also `dual_control`, `force_transfer_request_id`, `initiated_by_user_id`, `initiated_by_email`, `approved_by_user_id` |
| `workspace_ownership_transfer` | the actor: the owner handing on, an admin or a CLI run | the actor | `workspace:<id>` | `previous_owner_id`, `new_owner_id`, `ownership_epoch` |
| `workspace_purged` | the actor or the owner | the actor | `workspace:<id>` | `previous_owner_id`, `soft_deleted`, `contexts`, `memories`, `billing_contract_active` |
| `account_erasure` | the subject's pseudonym | the pseudonym of the subject's address | `user_pseudonym:<prefix>` | `request_id`, `is_self_service`, `initiated_by`, `reason_code` |
| `context_restore` | the actor | the actor | `context:<id>` | `workspace_id`, `name`, `renamed_from`, `deleted_at`, `deleted_by`, `memories_restored`, `memories_left_deleted`, `files_restored`, `file_bytes_restored` |
| `bm25_drift_reveal_terms` | the actor | the actor | `bm25_drift_log:<id>` | `outcome`, `context_id`, `reason`, `num_terms_resolved`, `num_terms_total` |
| `mcp_notice.created`, `mcp_notice.withdrawn` | the actor | the actor | `mcp_notice:<id>` | `text_chars`; a create also `starts_at`, `expires_at`, `plans` |
| `signup_blocked` | the subject | `oauth-callback` | `signup_gate:<provider>` | `provider` |
| `signup_admitted` | the subject | the sign-up path | `signup_gate:<provider>` | `provider`, `admission`, `invitations` (each `id`, `workspace_id`, `invited_by`), `invitation_count` |
| `beta_invite.redeemed` | the subject | `email-signup` or `oauth-callback` | `beta_invite:<id>` | `provider`, `inviter_user_id`, `allowlist_entry_id` |
| `sleep_run_trigger` | the actor | the actor | `context:<id>` | `target_user_id`, `workspace_id`, `context_id`, `report_id` |
| `sleep_merge_undo` | the actor | the actor | `sleep_report:<id>` | `target_user_id`, `workspace_id`, `context_id`, `report_id`, `action_id`, `restored_memory_id`, `winner_id` |

- `old_value` and `new_value` are the stored values for `system_role_update`, `system_admin_promote`, `system_admin_demote`, `workspace_slot_bonus_update` and `referral_grant_revoke` (roles and numbers), and `null` for every other action.
- `metadata` holds the keys of the table that the row has, each as stored when it is a string, a number, a boolean or null (`plans` a list of strings, `invitations` a list of objects with the three keys named). It is `null` for a row that stores none.
- Rows of other actions (a user's own account events, member and workspace events, system and CLI events) are not part of this read; asking for one with `action` is a `422`. Plan changes have their own read, `GET /api/v1/admin/plans/audit`.

### Service notices over MCP

Issue #2762. The notices delivered once per opted-in user in the MCP `bootstrap` reply ([Service notices over MCP](#service-notices-over-mcp)). The routes follow `SYSTEM_ADMIN_MODE` like every system-admin route (a service operator may call them) and work while `MCP_NOTICES_ENABLED` is off, so a notice can be prepared first. There is no edit and no delete: a correction is withdraw + create. Every create and withdraw writes an audit row (`mcp_notice.created` / `mcp_notice.withdrawn`, the actor, the SHA-256 of the text, never the text; a create also `plans`) and a WARNING log line `mcp_notice_created` / `mcp_notice_withdrawn` with the actor, the id and the character count (a create also `plans`).

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/admin/mcp-notices` | Write a notice. Body `{"text", "expires_at", "starts_at"?, "plans"?}` (no other key; times with a UTC offset). `201` with the notice. |
| `GET /api/v1/admin/mcp-notices?state=all&limit=50` | The notices, newest first: `{"notices": [...], "delivering": <MCP_NOTICES_ENABLED>}`. `state`: `active`, `scheduled`, `ended` (expired or withdrawn) or `all` (default); `limit` 1-100 (default 50). |
| `POST /api/v1/admin/mcp-notices/{notice_id}/withdraw` | End a notice now: every `bootstrap` that claims notices after the withdrawal commits skips it, while one already claiming at that moment may still deliver it once. Idempotent: an ended notice (withdrawn, or expired on its own) is returned unchanged and no audit row is written. |

A notice: `{"id", "text", "starts_at", "expires_at", "withdrawn_at", "created_at", "state", "delivered_count", "plans"}`; `delivered_count` is the number of people it was delivered to (an aggregate: no recipient is ever listed); `plans` is the list of tier keys it is for, in tier order, or `null` for everyone (notices written before Issue #2800 read `null`). A notice with `plans` is delivered only in a `bootstrap` call whose context is in a workspace on one of those plans, read at delivery, still once per person across all workspaces.

The rules, checked on create:

- **Text** (`422 MCPNOTICE-001`, `details.reason`): one line of plain text, 1-300 characters and at most 900 UTF-8 bytes after normalisation (Unicode NFC, the ideographic space read as a space, spaces trimmed and collapsed). Refused: control and formatting characters, line breaks, every space but U+0020 (`control_or_format_char`); invisible characters such as zero-width or variation selectors (`invisible_char`); `<` `>` `[` `]` `` ` `` `"` `\` `{` `}` `@` and HTML character references such as `&#64;` or `&lt;` (`markup_char`; a plain `&` as in `Q&A` passes); and anything a client could render as a link (`link`): a scheme with `//`, `//` followed by anything but a space (a protocol-relative link such as `//localhost`; also `a//b`), `mailto:` / `javascript:` / `data:` / `vbscript:` / `file:` / `tel:` / `sms:`, `www.`, a host name in any script such as `example.com`, `例え.jp` or `пример.рф` (also `README.md`, `Node.js`, `Ver.アップ`, or `。` with ASCII on both sides such as `Kagura。Kagura` or `MCP。Kagura`: reword them or put a space after `。`; `。` and `｡` count as a dot unless the character directly before or directly after them is Japanese — kanji, kana including `ー`, CJK punctuation, fullwidth and halfwidth forms — so `example。com`, `examplе。com` with a Cyrillic `е`, `test_。com`, `例えa。jp` and `пример。рф` are refused while 「…しました。Kagura」, 「対象は MCP。保存は…」 and 「接続先は MCP。」 pass; accepted residual: a label whose character next to `。` is Japanese, such as `例え。jp` or `exa例。com`, is read as prose, although a browser still resolves such a host; the ideographic numerals `〇` and `〡` do not count as Japanese, so `hell〇。com` is refused), an `xn--` label, an IPv4 or IPv6 address — also in their fullwidth forms and with combining marks inside them. Other reasons: `empty`, `too_long` (also any text over 1,200 characters before normalisation), `too_many_bytes`. The text is never echoed in an error.
- **Window** (`422 MCPNOTICE-002`, `details.reason`): `expires_at` after `starts_at` (`ends_before_start`) and in the future (`already_expired`); at most 30 days long (`too_long`); `starts_at` (default now) at most 90 days ahead (`starts_too_far_ahead`) and not more than 60 seconds in the past (`starts_in_the_past`).
- **Plans** (Issue #2800, `422 VAL-001` with `details.errors[].loc` starting `["body", "plans", ...]`): absent or `null` means everyone who opted in; otherwise a JSON array of one or more distinct tier keys (`free`, `basic`, `pro`, `promax`), compared exactly. Refused: `[]`, an unknown, miscased or padded name (`"enterprise"`, `"Pro"`, `" pro"`), a repeated name (for these `msg` lists the allowed keys), an entry that is not a string (`loc` `["body", "plans", 0]`) and a value that is not an array (pydantic's own `msg`). The input is never echoed. Stored and returned in tier order; a list of all four tiers stays a list (a tier added later is not in it). No edit: withdraw + create.
- **Caps**: at most 3 notices live or scheduled at a time (`409 MCPNOTICE-003`); at most 5 created in any 24 hours, withdrawn ones included (`409 MCPNOTICE-007`). Targeted notices count alike.

`POST .../withdraw` with an unknown id: `404 MCPNOTICE-004`.

### Sleep Maintenance

| Endpoint                                         | Purpose                                                       |
|--------------------------------------------------|---------------------------------------------------------------|
| `GET /api/v1/admin/sleep-reports`                | List reports with filters (`status`, `source`, `context_id`, `user_id`, `workspace_id`) and pagination. `source` is `sleep` (Sleep runs), `analysis` (Memory Analysis spend rows) or `all` (default); each row carries its `source` (#2219). `workspace_id` (#2844) answers only the reports whose stored workspace id is that UUID, ANDed with the other filters, and `total` counts only those; a value that is not a UUID is a `422`, an id with no report answers an empty list, and a report without a workspace id never matches. |
| `GET /api/v1/admin/sleep-reports/{report_id}`    | Fetch a single report with per-phase results and the full action audit log: `{"report", "actions", "action_count"}`. `?include_actions=false` (#2842) answers `actions: []` and reads no action; `action_count` is still the run's number of actions. |
| `GET /api/v1/admin/sleep-reports/{report_id}/actions` | One page of a report's actions (#2842): `{"actions", "total", "limit", "offset"}`. `limit` 1-500 (default 50) and `offset` from 0 to 2^63-1; a value outside is a `422`, as is a `report_id` that is not a UUID. Actions come in `id` order (the order the run recorded them, so a running report's new actions land on later pages) and carry the fields of the detail route's actions. `total` equals the detail route's `action_count`. An unknown report is a `404` on both routes. Read only. |
| `POST /api/v1/admin/sleep/run` | Run Sleep for the calling admin's own contexts. Body `{"context_id"?}`. `202 {"report_ids": [...]}`; `409 SLEEP-002` while the caller has a running report (a running report that no process is running and that was started more than 15 minutes ago no longer counts: it is marked `failed` as interrupted and the run is admitted, #2856); `404 SLEEP-003` when no context is eligible. Own-account: a service operator gets `403 SYSADMIN-004`. |
| `POST /api/v1/admin/sleep/actions/{action_id}/undo-merge` | Undo one dedup merge of the calling admin's own report. Own-account: a service operator gets `403 SYSADMIN-004`. |
| `POST /api/v1/admin/users/{user_id}/sleep/run` | Run Sleep once for a named user's context (#2838). Body `{"context_id": "<uuid>"}` (required, `422` without it). `202 {"report_ids": ["<uuid>"]}`: one report, whose `user_id` is the target user. A session admin and a service operator may call it. |
| `POST /api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge` | Undo one dedup merge of a named sleep report (#2838). No body. `200 {"restored_memory_id", "winner_id", "report_id", "undone_action_id"}` (ids only). A session admin and a service operator may call it. |

**Run for a user** (`POST /api/v1/admin/users/{user_id}/sleep/run`). The run is the one the scheduled sweep starts for `(user_id, the context's workspace, context_id)`, in the background; the run itself applies the workspace's Sleep state, as for the sweep. It executes inside the process that answers the request, with that process's LLM, embedding, vector-store and Sleep settings (as does the re-embedding of the undo below), so a `service` process needs the same settings as the public processes for these two routes. Checks, in this order:

- `404 RES-001`: no account with this `user_id`.
- `409 SLEEP-002` with `details.running_report_id` and `details.started_at`: the target user has a running report (a running report of the caller does not count). Best-effort under concurrent calls. A running report that no process is running and that was started more than 15 minutes ago no longer counts: it is marked `failed` as interrupted and the run is admitted (#2856).
- `404 SLEEP-003` with `details.context_id`: the context is not eligible. Eligible means the context is live, its `sleep_mode` is not `skip` and the user has a live memory in it. Every cause answers the same message.

The handler writes an `audit_logs` row in the report's commit: action `sleep_run_trigger`, the caller as actor (`operator:<address>` in `service` mode), resource `context:<context_id>`, metadata `{"target_user_id", "workspace_id", "context_id", "report_id"}`. The row records the request and stays when the run fails. A refusal writes nothing. The row is read back through `GET /api/v1/admin/audit-logs` (`action=sleep_run_trigger`, see [Audit log](#audit-log)).

**Undo for a report** (`POST /api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge`). The action is found by `action_id` and `report_id`, whoever the report belongs to. `404 SLEEP-UNDO-action_not_found` for an unknown action, an action of another report and an unknown report alike. Then the checks and answers of the own-account undo: `400 SLEEP-UNDO-not_a_merge`, `409 SLEEP-UNDO-already_restored`, `409 SLEEP-UNDO-edge_changed`, `409 SLEEP-UNDO-not_merge_deleted`, `410 SLEEP-UNDO-memory_purged`. A successful undo records:

- an `undo_merge` action on the same report, whose `undone_by` is the caller's `user_id` for a session admin and the fixed string `operator` for a service operator (the report's actions are read by workspace members; an operator's address is recorded in `audit_logs` only);
- the workspace's operation history event `sleep.undo` (without an actor for an operator);
- an `audit_logs` row: action `sleep_merge_undo`, the caller as actor, resource `sleep_report:<report_id>`, metadata `{"target_user_id", "workspace_id", "context_id", "report_id", "action_id", "restored_memory_id", "winner_id"}`, read back through `GET /api/v1/admin/audit-logs` (`action=sleep_merge_undo`).

Neither route answers, logs or audits memory text, tags, the target user's e-mail or name, or a context's name; the audit row names the caller, as stated above. The own-account undo writes no `audit_logs` row.

Each report, in the list rows and in the detail's `report`, carries `workspace_name` next to `workspace_id` (#2844): the workspace's name while it is live, `null` for a soft-deleted or missing workspace and for a report without a workspace id. One query resolves the names of a response; nothing else about the workspace (owner, members, plan) is answered. The workspace-scoped routes (`GET /api/v1/workspaces/{workspace_id}/sleep-reports` and its detail) share the model and fill it the same way.

See [Sleep Maintenance](sleep-maintenance.md) for the full Sleep cycle design, `sleep_mode`, and rollback semantics.

### Memory Health (deprecated)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/admin/memory-health` | **Deprecated** (#2764): an alias of [`GET /api/v1/memory-health`](#memory-health) that answers a system admin with the same document, with the same `context_id` parameter. It is removed in a later release; call the new path. A service operator gets `403 SYSADMIN-004` (it has no contexts of its own). |

### Environment Console

Read-only view of the deployment's environment-backed settings, behind the admin UI's Environment page. Every key is set via environment variables and applied on restart/redeploy — there is no runtime override.

| Endpoint                          | Purpose                                                       |
|-----------------------------------|---------------------------------------------------------------|
| `GET /api/v1/config`              | The **effective** value of each key (what the running process uses), as `{key, value, category, description, is_sensitive, read_only}`. `read_only` is `true` for every key. Any authenticated caller; the `hosted` category (BYOK / cost-display / plan-page flags, managed LLM lane, reranker defaults and endpoint) is returned to system admins only. Credentials embedded in a URL value are always masked (`https://***@host`). |
| `GET /api/v1/config/schema`       | Display metadata per key (type, description, `requires_restart`, impact, examples). |
| `PUT /api/v1/config/{key}`, `POST /api/v1/config/batch` | Always refused with `409` `CFG-002` (`details.keys` lists the refused keys); a batch is refused as a whole. Change the environment and restart/redeploy instead. |
| `GET /api/v1/admin/environment` | The same settings for an administrator (#2837): a session admin in `session`, a service operator in `service`, `404` in `off`, like every system-admin route. No query parameter. `{"configs": [...], "embedding_models": {...}}`, described below. |

`GET /api/v1/admin/environment` answers the values of the process that serves the request. Processes of one deployment can run with different environments (a `service` process beside public ones), so the answer describes that process only.

- `configs`: every key of the four categories (`embedding`, `search`, `system`, `hosted`), in that order, as `{"key", "category", "value", "type", "description", "requires_restart"}`. `value` is the effective value; `type`, `description` and `requires_restart` come from the key's schema entry and are null for a key without one. Credentials embedded in a URL inside any string value are masked (`https://***@host`). A key whose value is not answered carries the string `***MASKED***` as its `value`, whatever it is set to; every key of today's categories is answered.
- `embedding_models`: `{"default_model", "offered": [{"name", "dimensions", "provider"}]}`, the models the deployment offers (`EMBEDDING_MODEL_ALLOWLIST`) and its default. The route does not report whether a provider is reachable: it reads no stored key and calls no endpoint.

Rows written to the `config_overrides` table by earlier versions were never read by any runtime consumer; they are ignored.

### Neural Config

Sleep and Neural Memory tuning knobs (LLM provider, budgets, per-phase toggles, reranker weights) are persisted in `neural_config` and exposed under `/api/v1/admin/neural-config`. The fields are editable from the admin UI's Neural Config page.

### Embedding calibrations

| Endpoint | Purpose |
|----------|---------|
| `POST /api/v1/admin/neural/recalibrate?model=&dimensions=` | Start a model-global calibration for one embedding model and dimensions. Always `202` with `accepted` (`false` when a run for the pair is already in flight) and an echo of the pair. The answer does not say what the run wrote: read the rows below. |
| `GET /api/v1/admin/neural/calibrations` | The model-global calibration rows (#2845). Read only, no parameters, no paging: at most two rows (`knn_seed`, `edge_gate`) per model and dimensions. Follows `SYSTEM_ADMIN_MODE` like every system-admin route (a service operator may call it). |

```json
{
  "knn_seed_override": null,
  "calibrations": [
    {
      "model_name": "text-embedding-3-small",
      "dimensions": 512,
      "kind": "knn_seed",
      "p25": 0.41, "p50": 0.48, "p75": 0.55, "p90": 0.63, "p95": 0.67, "p99": 0.74,
      "sample_size": 10000,
      "sampled_at": "2026-11-01T03:00:00Z",
      "valid_until": "2026-12-01T03:00:00Z",
      "expired": false,
      "threshold": 0.63
    }
  ]
}
```

- `calibrations` is ordered by `model_name`, `dimensions`, `kind`. A pair that was never calibrated has no row. A run that writes nothing (no context uses the pair, or the sample was too small) leaves the pair's earlier rows as they are. A successful run replaces the pair's rows, so `sampled_at` tells when the values were computed and whether a run wrote.
- `kind`: `knn_seed` is the top-k neighbour distribution behind the k-NN seed edges written on ingest; `edge_gate` is the random-pair distribution behind the co-activation edge gate.
- `expired`: `valid_until` has passed. An expired row is still used at runtime.
- `threshold` is computed with the config of the process that answers (its `neural_config` values at that moment): for `knn_seed`, the larger of the row's `knn_seed_min_percentile` percentile and `knn_seed_min_similarity_floor`; for `edge_gate`, the larger of its `min_similarity_for_edge_percentile` percentile and `min_similarity_for_edge_floor`. Another process with a different config can give another value for the same row.
- `knn_seed_override` is `knn_seed_min_similarity`, or `null`. When it is set, it is the seeding threshold and the `knn_seed` rows are not used.
- The read starts no calibration, also for an expired row. Per-context rows are never answered, and the answer holds no row id and no context, workspace, memory or user identifier.

### Worker App Identities

System-admin (`role=admin`) lifecycle API for platform worker app identities (Slack / Discord / Teams bridge apps) — [#1315](https://github.com/kagura-ai/memory-cloud/issues/1315). Signing secrets are **write-only**: responses expose `has_active_secret` and revision metadata, never the secret or its ciphertext. Lifecycle mutations emit post-commit audit log events ([#1339](https://github.com/kagura-ai/memory-cloud/issues/1339)).

| Endpoint                                                          | Purpose                                                       |
|-------------------------------------------------------------------|---------------------------------------------------------------|
| `GET /api/v1/admin/worker-apps`                                   | List identities with lifecycle metadata (`status`, `revision`, active/retiring secret revisions). |
| `POST /api/v1/admin/worker-apps`                                  | Create an identity: `platform` (`slack`/`discord`/`teams`), `app_key`, `display_name`, `signing_secret`. |
| `PATCH /api/v1/admin/worker-apps/{platform}/{app_key}`            | Update `display_name` and/or `status` (`active` / `disabled`); at least one field required. |
| `POST /api/v1/admin/worker-apps/{platform}/{app_key}/rotate-secret` | Rotate the signing secret; the previous secret stays verifiable for `retiring_for_seconds` (default 3600). |

---

## MCP Tools

Kagura Memory Cloud provides MCP tools for memory, contexts, agents, files, resources, maintenance, and administration. See the [MCP Tools Reference](mcp-tools.md) for the full catalog, profiles, and required roles. The examples below illustrate commonly used tools; every other tool shares the same JSON-RPC call shape.

### Authentication and sessions on /mcp

`/mcp` (and `/mcp/w/{workspace_id}`) accepts an API key, an OAuth2 access token or a session cookie. A failed check answers with an RFC 6750 challenge that always names the protected-resource metadata (RFC 9728):

| Request | Status | `WWW-Authenticate` |
|---------|--------|--------------------|
| No credentials | `401` | `Bearer realm="Kagura Memory Cloud", resource_metadata="<origin>/.well-known/oauth-protected-resource"` — no error code (RFC 6750 §3.1) |
| Unknown, expired or revoked token, or an OAuth token issued for another resource | `401` | `Bearer realm="…", error="invalid_token", error_description="…", resource_metadata="…"` |
| Malformed `Authorization` header: not `Bearer <token>` (the scheme is case-insensitive), or an empty token or one containing whitespace | `401` | `Bearer realm="…", error="invalid_request", error_description="…", resource_metadata="…"` |
| OAuth token without the scope a `tools/call` needs | `403` | `Bearer realm="…", error="insufficient_scope", error_description="…", scope="<granted scopes plus the required one>", resource_metadata="…"` |

- **Credentials that cannot be checked.** When the server cannot look a credential up (the database is unreachable, or the lookup fails unexpectedly) it answers `503` with `Retry-After: 5`, no `WWW-Authenticate` and `{"error": "temporarily_unavailable", "error_description": "Could not verify credentials right now; retry shortly.", "correlation_id": "…"}`. The credential may well be valid: retry the request instead of re-authorizing. A failed workspace membership check on `/mcp/w/{workspace_id}` answers the same way.
- **Audience (RFC 8707).** An OAuth access token bound to a resource must be bound to this server's MCP resource. Every form the [resource rule](#authorization-code-grant-mcp-clients) accepts qualifies: `<origin>/mcp`, `<origin>/mcp/` and paths beneath it such as `/mcp/w/{workspace_id}`, the default port given or left out, the host in any case, any query. The rule applies on `/mcp/w/{workspace_id}` too. A token issued without `resource` is accepted.
- **Scope.** `memory:read` or `memory:write` per tool, checked on `tools/call` only; see [MCP Tools › OAuth scopes](mcp-tools.md#oauth-scopes). API keys, agent-bound keys and session cookies are not scope-checked.
- **Origin.** Checked before authentication on every `/mcp` request. A request without `Origin` (server-side clients such as the Claude and ChatGPT backends, CLI clients) is served. One whose `Origin` is not `CORS_ORIGINS`, the `FRONTEND_URL` origin or an entry of `MCP_ALLOWED_ORIGINS` — including `null` — gets `403` with the JSON-RPC error `-32600` "Forbidden: the request Origin is not allowed for this MCP server".
- **Sessions (session-based Streamable HTTP)** on `/mcp`, `/mcp/` and `/mcp/w/{workspace_id}`:
  - `POST` without `Mcp-Session-Id` opens a session under an id the server chooses (returned in `Mcp-Session-Id`; `mcp-` followed by 16 lowercase hex digits).
  - `POST` naming a session of the caller uses it. One the server does not hold (expired after an hour idle, or lost on a restart or deploy) is re-adopted for the authenticated caller under the same id, so a client keeps working without re-initializing. Only an id of the server-minted shape is re-adopted; any other id was never issued and gets `404`.
  - A session id opened by another user or in another workspace gets `404` with the JSON-RPC error "This session id cannot be used by this connection. Send a new initialize request without Mcp-Session-Id." Such a request does not refresh that session's idle timer.
  - `DELETE` with the caller's `Mcp-Session-Id` ends that session (`204`); an unknown or foreign id gets the same `404`, a request without one `400`. The ended id then gets `404` from every caller until the idle timeout (`MCP_SESSION_TIMEOUT_SECONDS`, default one hour) has passed; the record is kept in process memory, so a restart forgets it.
  - `initialize` echoes `2025-03-26` or `2024-11-05` when the client asks for one of them, and answers any other requested version with `2025-03-26`.
  - A session that negotiated `2025-03-26` accepts JSON-RPC batch arrays (at most 50 messages): the answers to its requests come back as one array, an array of notifications and responses only gets `202`, and an array containing `initialize` or an empty one gets `400` `-32600`. A batch in which a `tools/call` was refused for scope is answered `403` with the `insufficient_scope` challenge; its other messages still ran. A `2024-11-05` session, or one re-adopted after a restart, answers a batch with `400` `-32600`.
  - `GET` gets `405` (`Allow: POST, DELETE`), the Streamable HTTP answer of a server that offers no server-initiated SSE stream. It never opens or re-adopts a session. A `GET` naming a session that was ended by `DELETE`, belongs to another user or workspace, or was never issued still gets the `404` above.
  - Other methods get `405` (`Allow: POST, DELETE`) and other paths `404`. The removed SSE transport answers `410`: `GET /mcp/sse` and `POST /mcp/messages/…`. None of them reads or opens a session.
  - Stateless MCP 2026-07-28 requests have no session and ignore an `Mcp-Session-Id` header.

### 1. remember

Store a new memory.

```python
# MCP Tool Call
{
  "name": "remember",
  "arguments": {
    "summary": "User prefers TDD approach",
    "content": "Always write tests first...",
    "type": "preference",
    "tags": ["testing", "workflow"],
    "importance": 0.9
  }
}
```

### 2. recall

Search memories.

```python
{
  "name": "recall",
  "arguments": {
    "query": "How to implement OAuth2?",
    "k": 5,
    "use_rerank": false
  }
}
```

The top `full` results (default 3; `0` for summaries only) also carry `content`, up to 1,500 characters each, with `content_truncated` / `content_next_offset` when cut — see [MCP Tools › recall](mcp-tools.md#recall).

### 3. reference

Get full memory details. The response is bounded to `max_chars` characters (default 20,000); a larger memory comes back with explicit truncation markers and is read in pages via `content_offset` / `details_offset` / `context_offset` — see [MCP Tools › reference](mcp-tools.md#reference).

```python
{
  "name": "reference",
  "arguments": {
    "memory_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

### 4. forget

Delete memories.

```python
{
  "name": "forget",
  "arguments": {
    "memory_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

### 5. explore

Discover related memories via graph traversal.

```python
{
  "name": "explore",
  "arguments": {
    "memory_id": "550e8400-e29b-41d4-a716-446655440000",
    "depth": 2,
    "min_weight": 0.5
  }
}
```

### 6. list_my_bindings

List your public-bound API keys (read-only introspection, Issue #629). Returns the bindings you own — keys attributed to a single public context for per-key rate-limit, audit, and revoke. Revoked keys are excluded; `key_prefix` is omitted (use `describe_binding`). Takes no arguments.

```python
{
  "name": "list_my_bindings",
  "arguments": {}
}
# → {"status": "success", "count": 1, "bindings": [
#     {"key_id": "akey_3fJ0kQ9pLm2ZtX7cVb1NaR", "name": "slack-bot", "context_id": "…",
#      "context_name": "Slack Bot", "created_at": "2026-06-01T12:00:00Z"}]}
```

### 7. describe_binding

Describe one of your bindings by **exactly one** of `key_id` (the key's public id, `akey_…`, from `list_my_bindings`) or `context_id` (UUID). An integer `key_id` returns `invalid_arguments`. The result is scoped to keys you own; an unknown or not-yours selector returns a uniform `binding_not_found`. Adds `key_prefix` to the `list_my_bindings` shape. No secret is ever returned.

```python
{
  "name": "describe_binding",
  "arguments": { "key_id": "akey_3fJ0kQ9pLm2ZtX7cVb1NaR" }   # OR { "context_id": "<uuid>" } — not both
}
# → {"status": "success", "binding": {
#     "key_id": "akey_3fJ0kQ9pLm2ZtX7cVb1NaR", "name": "slack-bot", "context_id": "…",
#     "context_name": "Slack Bot", "created_at": "…", "key_prefix": "kagura_pub_…"}}
```

> **Read-only boundary:** minting and revoking bindings stay on the SDK / CLI / HTTP API / dashboard (design decision from #626). Public-bound API keys cannot call any MCP tool — they are rejected at MCP authentication — so these introspection tools are only reachable by the binding's **owner** via a session or workspace-scoped key.

### Agent Substrate tools

These tools back the [Agent Memory Substrate](concepts.md#agent-memory-substrate). `load_pinned`, `load_guardrails` and `recall_upcoming` are deterministic delivery reads; `set_state` / `get_state` drive the agent state lane; `feedback` records the retrieval signal.

#### 8. load_pinned

Deterministically load a context's always-load memories (`delivery_mode="always"`) — the complete, unranked set, every call. The deterministic counterpart to probabilistic `recall()`; use it for notes that stay relevant every turn, such as an agent's goal or a standing decision.

```python
{
  "name": "load_pinned",
  "arguments": { "context_id": "550e8400-..." }
}
```

#### 9. load_guardrails

Deterministically load a context's **guardrail set** for a client-side hook: the trusted-tier pinned set plus the memories marked with `details.tool_trigger`, each lane ordered `importance DESC, created_at ASC, id ASC` and capped on its own (`cap` bounds `tool_triggered` only). Read-only, rate-limit exempt, plain SQL. The stored patterns are returned as data — the server validates them on write and never runs them. Full contract, error codes and the shared cache format: [MCP Tools › Tool guardrails](mcp-tools.md#tool-guardrails).

```python
{
  "name": "load_guardrails",
  "arguments": { "context_id": "550e8400-...", "cap": 50 }
}
```

Returns `{status, format, version, pinned: [item], tool_triggered: [item], total_available, truncated, cap, pinned_cap, pinned_total_available, pinned_truncated, tool_triggered_total_available, tool_triggered_truncated, context_id, ...}`; `item = {memory_id, summary, context_summary, type, importance, delivery_mode, tool_trigger|null, source_type, authored_by_caller, created_at, updated_at}`.

#### 10. recall_upcoming

List forward-looking Time Memories (`type="time"` — the lane is keyed on the type; the write path never sets `delivery_mode="on_trigger"`) whose scheduled window is upcoming — deadlines, dated follow-ups. A deterministic time query, not semantic search.

```python
{
  "name": "recall_upcoming",
  "arguments": { "context_id": "550e8400-...", "from": "now" }
}
```

Each item is `{memory_id, summary, type, trigger}`, where `trigger` is the memory's `details.trigger`. Pass `"include_details": true` to get the full `details` object per item instead (it contains the trigger); for a single memory, `reference(memory_id)` is the cheaper way to read it in full.

#### 11. recall_nearby

List memories near a geographic point (`details.location`), nearest first with `distance_m`. A deterministic spatial query over stored coordinates — the WHERE-axis twin of `recall_upcoming`, not semantic search. Store a location with `remember(details={"location": {"lat": 35.68, "lon": 139.76, "label": "optional"}})` — `lat`/`lon` must be JSON numbers (validated server-side), and any memory type can carry one.

```python
{
  "name": "recall_nearby",
  "arguments": { "context_id": "550e8400-...", "lat": 35.6812, "lon": 139.7671, "radius_m": 1000 }
}
```

#### 12. set_state

Upsert agent scratch state (excluded from `recall()`). Requires `Editor`.

```python
{
  "name": "set_state",
  "arguments": {
    "context_id": "550e8400-...",
    "key": "current_task",
    "value": {"step": 3, "plan": "refactor auth"},
    "ttl_seconds": 3600
  }
}
```

#### 13. get_state

Read one key, or omit `key` to list all live state for the context.

```python
{
  "name": "get_state",
  "arguments": { "context_id": "550e8400-...", "key": "current_task" }
}
```

#### 14. feedback

Record whether a recalled memory was helpful (read-adjacent; any `Viewer` may call). Append-only; **collected but not auto-acted-on** (see [eval gate](eval/retrieval-feedback-and-eval-gate.md)).

```python
{
  "name": "feedback",
  "arguments": {
    "context_id": "550e8400-...",
    "memory_id": "660e8400-...",
    "helpful": true,
    "query": "how do I rotate JWT refresh tokens?"
  }
}
```

### Agent Control Plane tools (preview)

| Tool | Purpose |
|---|---|
| `register_agent` / `list_agents` / `get_agent` / `update_agent` / `delete_agent` | Workspace Agent Registry CRUD (Owner/Admin) |
| `bind_agent_context` / `list_agent_bindings` / `update_agent_binding` / `unbind_agent_context` | Purely subtractive context policy (Owner/Admin) |
| `get_agent_bootstrap` | Fail-soft composition of context info + pinned + optional trusted recall + upcoming + state; identity and authorization fail closed |

The control-plane tools use the same JSON-RPC shape as the examples above. Their REST companions and the current preview limitations are documented in [Agent Control Plane APIs](#agent-control-plane-apis-v0490-preview).

---

## Rate Limits

- **API Keys**: 1000 requests/hour
- **OAuth2 Tokens**: 500 requests/hour
- **Web Sessions**: 100 requests/hour

---

## Error Responses

All errors follow this format:

```json
{
  "detail": "Error message description",
  "status_code": 400
}
```

**Common Status Codes:**

- `400 Bad Request` - Invalid request parameters
- `401 Unauthorized` - Missing or invalid authentication
- `403 Forbidden` - Insufficient permissions
- `404 Not Found` - Resource not found
- `429 Too Many Requests` - Rate limit exceeded
- `500 Internal Server Error` - Server error

### Gate refusals

A refusal caused by the workspace's **plan**, a **quota**, a rollout **allowlist** or a
**deployment** switch carries a machine-readable `details` block, so a client never has to
read the message text or guess from the status code. These refusals use the structured
envelope — `{"error": ..., "message": ..., "details": {...}}` — enumerated in
[`api-surface-1.0/error-responses.md`](api-surface-1.0/error-responses.md#gate-refusals-1644).

Feature refusal (`FEAT-001`, HTTP 403):

```json
{
  "error": "FEAT-001",
  "message": "Feature 'team_invitations' not available on M plan. Upgrade to L plan to access this feature.",
  "details": {
    "gate": "plan",
    "feature": "team_invitations",
    "required_plan": "pro",
    "required_plan_display": "L",
    "current_plan": "basic"
  }
}
```

Quota refusal (`QUOTA-001`, HTTP 429 — or 403 on the resource-token and connector-seat caps,
which have always answered 403):

```json
{
  "error": "QUOTA-001",
  "message": "Context limit reached. Your S plan allows 1 context(s) per workspace. Upgrade to M plan for more contexts.",
  "details": {
    "gate": "quota",
    "quota_type": "contexts",
    "current": 1,
    "limit": 1,
    "required_plan": "basic",
    "required_plan_display": "M",
    "current_plan": "free"
  }
}
```

Reading them:

- **`details.gate`** says *why*. `plan` means a higher tier lifts it. `quota` means a cap was
  reached. `allowlist` means a rollout kill switch and `deployment` means the operator turned
  the feature off — neither of those can be lifted by any tier, so do not offer an upgrade for
  them. Role refusals are `AUTH-101` and carry no details at all.
- **`required_plan`** is the tier **key** to branch on; **`required_plan_display`** is the
  label to render if you have no tier list of your own. Both may be `null`, which means no
  tier lifts this refusal — say so rather than inventing an upgrade.
- **`current` / `limit`** are integers and are absent on the two families that have no integer
  counts: the BYOK embedding spend caps (`QUOTA-002`, which ship `cap_usd` / `current_usd`)
  and the daily API quotas. Render a number-free message when they are missing.
- Pre-existing detail fields (`used_today`, `owned_count`, `max_connectors`, …) are unchanged
  and still shipped; the canonical names were added beside them.
- A `QUOTA-001` with no `gate` and no `quota_type` — the 1 MB memory-size limit, for one — is
  **not** a plan quota: no tier lifts it, so show the message and no upgrade.
- MCP tools return the same keys as top-level fields of their error envelope.

---

## SDKs and Examples

- **Python SDK**: [`kagura-memory-python-sdk`](https://github.com/kagura-ai/kagura-memory-python-sdk) — `KaguraClient` (MCP JSON-RPC) plus REST clients (`ResourceClient`, `FilesClient`, `SecretClient`, `WorkspaceClient`, `AgentsClient`) and a document `FileIngestor`. Supports OAuth2 device flow via `kagura auth login` against the pre-seeded `kagura-cli` public client.
- **JavaScript SDK**: Planned
- **Example Code**: [README Quick Start](../README.md#quick-start) and the [Python SDK](https://github.com/kagura-ai/kagura-memory-python-sdk)

---

## Support

- **GitHub Issues**: [Report bugs](https://github.com/kagura-ai/memory-cloud/issues)
- **Documentation**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **OpenAPI Spec**: `http://localhost:8080/openapi.json`
