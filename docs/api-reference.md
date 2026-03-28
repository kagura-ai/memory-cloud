# API Reference

Kagura Memory Cloud provides both REST APIs and MCP (Model Context Protocol) tools for AI memory management.

## Overview

- **REST API Base URL**: `http://localhost:8080/api/v1`
- **MCP Server Endpoint**: `http://localhost:8080/mcp/sse`
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
  "use_rerank": true
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Natural language search query |
| `k` | integer | No | Number of results (default: 5, max: 100) |
| `filters` | object | No | Filter by type, tags, importance |
| `use_rerank` | boolean | No | Enable Cohere reranking (default: true) |

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
    "use_rerank": true
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

System information.

**Response:**

```json
{
  "version": "0.5.1",
  "environment": "production",
  "features": {
    "neural_memory": true,
    "hybrid_search": true,
    "oauth2": true
  }
}
```

---

## MCP Tools

Kagura Memory Cloud provides 5 MCP tools for AI assistants:

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
    "use_rerank": true
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

- **Python SDK**: Coming soon
- **JavaScript SDK**: Coming soon
- **Example Code**: [GitHub Repository](https://github.com/kagura-ai/memory-cloud/tree/main/examples)

---

## Support

- **GitHub Issues**: [Report bugs](https://github.com/kagura-ai/memory-cloud/issues)
- **Documentation**: [http://localhost:8080/docs](http://localhost:8080/docs)
- **OpenAPI Spec**: [openapi.json](../api/openapi.json)
