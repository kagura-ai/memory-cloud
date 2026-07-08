# API Reference

Kagura Memory Cloud provides both REST APIs and MCP (Model Context Protocol) tools for AI memory management.

## Overview

- **REST API Base URL**: `http://localhost:8080/api/v1`
- **MCP Server Endpoint**: `http://localhost:8080/mcp/w/{WORKSPACE_ID}` (Streamable HTTP transport)
- **OpenAPI Specification**: [openapi.json](../api/openapi.json)

## Authentication

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

### 2. OAuth2 Access Token

```bash
curl -H "Authorization: Bearer <access_token>" \
  http://localhost:8080/api/v1/memory/recall
```

### 3. Session Cookie (Web UI)

Session-based authentication for the web management interface.

---

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

See [Chunking Guide](/docs/chunking-guide.md) for comprehensive examples and anti-patterns.

---

### POST /api/v1/memory/recall

Search memories using Hybrid Search (60% semantic + 40% BM25) with optional Neural Memory boosting.

**Request Body:**

```json
{
  "query": "How do I implement authentication in FastAPI?",
  "k": 10,
  "filters": {
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
| `filters` | object | No | Filter by type, tags, importance, date ranges. `tags_match: "all"` for AND logic. Date: `created_after`, `created_before`, `updated_after`, `updated_before` (ISO 8601). **Trust:** `trust_tier: "trusted"` excludes `external`-tier contexts and `connector`-sourced memories — pass it for behaviour-influencing reads (see [Trust tier](concepts.md#agent-memory-substrate)) |
| `use_rerank` | boolean | No | Request reranking (default: false). Only effective if reranking is also enabled in the context's search config and a provider (Voyage/Cohere) is configured. |

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

**Example (curl):**

```bash
curl -X POST http://localhost:8080/api/v1/memory/recall \
  -H "Authorization: Bearer kagura_xxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "neural memory implementation",
    "k": 5,
    "use_rerank": false
  }'
```

---

### GET /api/v1/memory/reference/{memory_id}

Retrieve complete details (Layer 3) of a specific memory by ID.

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `memory_id` | string (UUID) | Yes | Memory ID from recall results |

**Response:**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
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
  "updated_at": "2025-11-20T15:00:00Z"
}
```

**Example (Python):**

```python
memory_id = "550e8400-e29b-41d4-a716-446655440000"
response = requests.get(
    f"http://localhost:8080/api/v1/memory/reference/{memory_id}",
    headers={"Authorization": "Bearer kagura_xxxxxxxxxxxx"}
)
print(response.json()["content"])
```

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
| `usage_guide` | string | No | LLM-oriented usage guidelines |
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

---

## Public Read API

`POST /api/v1/public/{context_id}/search` and `GET /api/v1/public/{context_id}/info` expose `is_public=true` contexts to anonymous and attributed callers (Issue #238, extended by Issue #626).

### Anonymous access (no Authorization header)

```bash
curl -X POST https://memory.kagura-ai.com/api/v1/public/CTX_UUID/search \
  -H "Content-Type: application/json" \
  -d '{"query": "What is Hebbian learning", "limit": 3}'
```

Rate-limited to **50 requests/minute per context**, shared across all anonymous callers. The bucket is `public_search:{ctx}:minute` in Redis. The shared quota means a single noisy client can saturate the bucket — that's the gap public-bound API keys fill.

### Attributed access (with public-bound API key)

```bash
curl -X POST https://memory.kagura-ai.com/api/v1/public/CTX_UUID/search \
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

### Device Authorization Grant (CLI / SDK login)

The Kagura Memory Python SDK (and any future first-party CLI) uses the
pre-registered `kagura-cli` public OAuth2 client to drive RFC 8628
device authorization grant — no client registration step is required
from the end user. From the terminal, `kagura auth login` does roughly:

1. SDK calls `POST /api/v1/oauth2/device/authorize` with
   `client_id=kagura-cli`.
2. The server returns a `verification_uri` plus a short `user_code`.
3. The user opens `verification_uri` in a browser, signs in to Kagura
   Memory, picks the workspace they want to grant access to, and
   approves the consent screen.
4. The SDK polls `POST /api/v1/oauth2/token` (with
   `grant_type=urn:ietf:params:oauth:grant-type:device_code`) and
   receives an `access_token` plus a `refresh_token` scoped to the
   chosen (user × workspace).
5. `kagura auth refresh` exchanges the refresh token for a new pair
   (refresh-token rotation is enforced server-side per RFC 6819
   §5.2.2.3 — the old access/refresh pair is revoked when a new pair
   is issued).
6. `kagura auth logout` calls `POST /api/v1/oauth2/revoke` to revoke
   the issued tokens.

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

## API Keys Management

### POST /api/v1/config/api-keys

Create a new API key (Admin only).

**Request Body:**

```json
{
  "name": "Production API Key",
  "scopes": ["memory:read", "memory:write"],
  "expires_at": "2026-11-22T00:00:00Z"
}
```

**Response:**

```json
{
  "id": 1,
  "name": "Production API Key",
  "key": "kagura_abc123xyz789",
  "scopes": ["memory:read", "memory:write"],
  "created_at": "2025-11-22T10:00:00Z",
  "expires_at": "2026-11-22T00:00:00Z"
}
```

⚠️ **Important**: The `key` value is shown only once. Store it securely.

---

### GET /api/v1/config/api-keys

List all API keys (Admin only).

**Response:**

```json
{
  "keys": [
    {
      "id": 1,
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

### DELETE /api/v1/config/api-keys/{key_id}

Permanently delete an API key (Admin only).

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

### GET /api/v1/info

System information. The `version` reflects the running server (current stable: see [GitHub Releases](https://github.com/kagura-ai/memory-cloud/releases)).

**Response:**

```json
{
  "version": "<server_version>",
  "environment": "production",
  "features": {
    "neural_memory": true,
    "hybrid_search": true,
    "oauth2": true,
    "sleep_maintenance": true
  }
}
```

---

## Admin APIs

Admin endpoints require `system_admin` or `workspace_admin` role.

### Sleep Maintenance

| Endpoint                                         | Purpose                                                       |
|--------------------------------------------------|---------------------------------------------------------------|
| `GET /api/v1/admin/sleep-reports`                | List Sleep runs with filters (`status`, `context_id`, `user_id`) and pagination. |
| `GET /api/v1/admin/sleep-reports/{report_id}`    | Fetch a single report with per-phase results and the full action audit log. |

See [Sleep Maintenance](sleep-maintenance.md) for the full Sleep cycle design, `sleep_mode`, and rollback semantics.

### Memory Health

| Endpoint                          | Purpose                                                       |
|-----------------------------------|---------------------------------------------------------------|
| `GET /api/v1/admin/memory-health` | Consolidated self-diagnosis report — consolidation / graph / retrieval sections graded `ok`/`warn`/`fail` from Sleep telemetry, graph invariants, and usage stats. Self-scoped (the calling admin's data partition). |

See [Memory Health Report](ops/memory-health-report.md) for every metric and threshold.

### Neural Config

Sleep and Neural Memory tuning knobs (LLM provider, budgets, per-phase toggles, reranker weights) are persisted in `neural_config` and exposed under `/api/v1/admin/neural-config`. The fields are editable from the admin UI's Neural Config page.

---

## MCP Tools

Kagura Memory Cloud provides 50 MCP tools for AI assistants across 12 categories (Memory, Agent Substrate, Neural Edges, Contexts, Tags, Files / R2, Analyses, Resources, Secrets, Sleep Maintenance, Usage, API-Key Bindings). See [README › MCP Tools](../README.md#mcp-tools) for the full table with required roles. The examples below illustrate the most commonly used tools; every other tool shares the same JSON-RPC call shape.

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

### 3. reference

Get full memory details.

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
#     {"key_id": 7, "name": "slack-bot", "context_id": "…",
#      "context_name": "Slack Bot", "created_at": "2026-06-01T12:00:00Z"}]}
```

### 7. describe_binding

Describe one of your bindings by **exactly one** of `key_id` (integer) or `context_id` (UUID). The result is scoped to keys you own; an unknown or not-yours selector returns a uniform `binding_not_found`. Adds `key_prefix` to the `list_my_bindings` shape. No secret is ever returned.

```python
{
  "name": "describe_binding",
  "arguments": { "key_id": 7 }   # OR { "context_id": "<uuid>" } — not both
}
# → {"status": "success", "binding": {
#     "key_id": 7, "name": "slack-bot", "context_id": "…",
#     "context_name": "Slack Bot", "created_at": "…", "key_prefix": "kagura_pub_…"}}
```

> **Read-only boundary:** minting and revoking bindings stay on the SDK / CLI / HTTP API / dashboard (design decision from #626). Public-bound API keys cannot call any MCP tool — they are rejected at MCP authentication — so these introspection tools are only reachable by the binding's **owner** via a session or workspace-scoped key.

### Agent Substrate tools

These tools back the [Agent Memory Substrate](concepts.md#agent-memory-substrate). `load_pinned` and `recall_upcoming` are delivery-mode-aware reads; `set_state` / `get_state` drive the agent state lane; `feedback` records the retrieval signal.

#### 8. load_pinned

Deterministically load a context's always-load memories (`delivery_mode="always"`) — the complete, unranked set, every call. The deterministic counterpart to probabilistic `recall()`; use it for an agent's Goal / Guardrail / critical policy.

```python
{
  "name": "load_pinned",
  "arguments": { "context_id": "550e8400-..." }
}
```

#### 9. recall_upcoming

List forward-looking Time Memories (`type="time"`, `delivery_mode="on_trigger"`) whose scheduled window is upcoming — deadlines, dated follow-ups. A deterministic time query, not semantic search.

```python
{
  "name": "recall_upcoming",
  "arguments": { "context_id": "550e8400-...", "from": "now" }
}
```

#### 10. set_state

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

#### 11. get_state

Read one key, or omit `key` to list all live state for the context.

```python
{
  "name": "get_state",
  "arguments": { "context_id": "550e8400-...", "key": "current_task" }
}
```

#### 12. feedback

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

---

## SDKs and Examples

- **Python SDK**: [`kagura-memory-python-sdk`](https://github.com/kagura-ai/kagura-memory-python-sdk) — `KaguraClient` (sync HTTP client) and `KaguraAgent` (LLM agent integration). Supports OAuth2 device flow via `kagura auth login` against the pre-seeded `kagura-cli` public client.
- **JavaScript SDK**: Planned
- **Example Code**: [GitHub Repository](https://github.com/kagura-ai/memory-cloud/tree/main/examples)

---

## Support

- **GitHub Issues**: [Report bugs](https://github.com/kagura-ai/memory-cloud/issues)
- **Documentation**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **OpenAPI Spec**: [openapi.json](../api/openapi.json)
