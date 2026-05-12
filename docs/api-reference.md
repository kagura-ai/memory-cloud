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
| `filters` | object | No | Filter by type, tags, importance, date ranges. `tags_match: "all"` for AND logic. Date: `created_after`, `created_before`, `updated_after`, `updated_before` (ISO 8601) |
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

### Neural Config

Sleep and Neural Memory tuning knobs (LLM provider, budgets, per-phase toggles, reranker weights) are persisted in `neural_config` and exposed under `/api/v1/admin/neural-config`. The fields are editable from the admin UI's Neural Config page.

---

## MCP Tools

Kagura Memory Cloud provides 37 MCP tools for AI assistants across 9 categories (Memory, Neural Edges, Contexts, Tags, Files / R2, Analyses, Resources, Sleep Maintenance, Usage). See [README › MCP Tools](../README.md#mcp-tools) for the full table with required roles. The examples below illustrate the most commonly used tools; every other tool shares the same JSON-RPC call shape.

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
