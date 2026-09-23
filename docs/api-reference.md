# API Reference

Kagura Memory Cloud provides both REST APIs and MCP (Model Context Protocol) tools for AI memory management.

## Overview

- **REST API Base URL**: `http://localhost:8080/api/v1`
- **MCP Server Endpoint**: `http://localhost:8080/mcp/w/{WORKSPACE_ID}` (Streamable HTTP transport)
- **OpenAPI Specification**: `http://localhost:8080/openapi.json`

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
| `profile`, `tools` | `target=instructions` only: the MCP URL's values, so the truncation suffix names the same tool the URL's `tools/list` shows |

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
| `POST /api/v1/agents/{agent_id}/bootstrap` | Compose context guide, pinned, optional trusted recall, upcoming, and state for session start |

Owner-provisioned member keys are minted through `POST /api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys`; supplying `agent_id` attaches the registered agent. Agent-bound keys for `suspended` or `retired` agents fail verification. In `enforce` mode, requests to unbound contexts use the same not-found shape as inaccessible contexts.

For registered ranking evaluations, the optional `recall_evaluation` object accepts a
deterministic `seed`, an exact `exploration_floor`, and `candidate_pool_k` (1–100, at least
`recall_k`). The successful recall component then adds identity-only
`selection_probabilities` for the complete authorized trusted candidate pool and a stamped
`selection_policy`. Ordinary bootstrap clients are unchanged when the object is omitted;
component errors never include this evidence.

> **Preview boundary:** `allowed_memory_types` and `allowed_source_types` are enforced per read-lane row as of [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299) (`null` = all, `[]` = deny-all) on the memory-read lanes (recall, reference, forget, explore, load_pinned, upcoming) for enforce-mode agents; shadow mode records `would_deny` without filtering. REST and MCP accept W3C `traceparent` and baggage keys `gen_ai.agent.id`, `gen_ai.conversation.id` (or `session.id`), and `kagura.agent.run.id`; invalid advisory values are dropped and credential-bound agent identity always wins. Server-side span export is not part of P0. The append-only `memory_access_events` table and writer cover bootstrap, load-pinned, feedback, recall, reference, remember, update, and forget emission with binding deny capture.

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
  "id": 12,
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
- `quota` and `remaining` are `null` for a system admin (unlimited).
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
  "search_defaults": {
    "use_rerank": false,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2"
  },
  "features": {
    "neural_memory": true,
    "research_tools": false,
    "plan_page": false,
    "byok": true,
    "cost_display": true,
    "managed_connectors": false,
    "managed_llm": false,
    "referrals": false,
    "beta_invites": false,
    "reranking": true
  }
}
```

`search_defaults` are the reranker values new contexts are created with (provider and model names only). Each `features` flag mirrors a deployment setting (`ENABLE_*`, `MANAGED_LLM_PROVIDER`); the values above are illustrative, not the defaults of every deployment.

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
| `GET /api/v1/admin/memory-health` | Per-context self-diagnosis breakdown — one graded entry per owned context (consolidation / graph / retrieval statuses, `ok`/`warn`/`fail`) from Sleep telemetry, graph invariants, and usage stats. Self-scoped (the calling admin's data partition). |
| `GET /api/v1/admin/memory-health?context_id=<uuid>` | The 3-section detail document for one owned context (un-owned → 404). `context_id=unattributed` targets signals recorded without a context. |

See [Memory Health Report](ops/memory-health-report.md) for every metric and threshold.

### Environment Console

Read-only view of the deployment's environment-backed settings, behind the admin UI's Environment page. Every key is set via environment variables and applied on restart/redeploy — there is no runtime override.

| Endpoint                          | Purpose                                                       |
|-----------------------------------|---------------------------------------------------------------|
| `GET /api/v1/config`              | The **effective** value of each key (what the running process uses), as `{key, value, category, description, is_sensitive, read_only}`. `read_only` is `true` for every key. Any authenticated caller; the `hosted` category (BYOK / cost-display / plan-page flags, managed LLM lane, reranker defaults and endpoint) is returned to system admins only. Credentials embedded in a URL value are always masked (`https://***@host`). |
| `GET /api/v1/config/schema`       | Display metadata per key (type, description, `requires_restart`, impact, examples). |
| `PUT /api/v1/config/{key}`, `POST /api/v1/config/batch` | Always refused with `409` `CFG-002` (`details.keys` lists the refused keys); a batch is refused as a whole. Change the environment and restart/redeploy instead. |

Rows written to the `config_overrides` table by earlier versions were never read by any runtime consumer; they are ignored.

### Neural Config

Sleep and Neural Memory tuning knobs (LLM provider, budgets, per-phase toggles, reranker weights) are persisted in `neural_config` and exposed under `/api/v1/admin/neural-config`. The fields are editable from the admin UI's Neural Config page.

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

Kagura Memory Cloud provides 64 MCP tools for AI assistants across 13 categories (Memory, Agent Substrate, Agent Control Plane, Neural Edges, Contexts, Tags, Files / R2, Analyses, Resources, Secrets, Sleep Maintenance, Usage, API-Key Bindings). See [README › MCP Tools](../README.md#mcp-tools) for the full table with required roles. The examples below illustrate the most commonly used tools; every other tool shares the same JSON-RPC call shape.

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

These tools back the [Agent Memory Substrate](concepts.md#agent-memory-substrate). `load_pinned`, `load_guardrails` and `recall_upcoming` are deterministic delivery reads; `set_state` / `get_state` drive the agent state lane; `feedback` records the retrieval signal.

#### 8. load_pinned

Deterministically load a context's always-load memories (`delivery_mode="always"`) — the complete, unranked set, every call. The deterministic counterpart to probabilistic `recall()`; use it for an agent's Goal / Guardrail / critical policy.

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
| `get_agent_bootstrap` | Fail-soft composition of context guide + pinned + optional trusted recall + upcoming + state; identity and authorization fail closed |

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
