# Core Concepts

This document explains the fundamental concepts of Kagura Memory Cloud.

## Workspace

A **Workspace** is the top-level organizational unit — think of it as a team or organization.

- All resources (contexts, memories, members) belong to a workspace
- Plan limits (memory count, API quotas, context count) are enforced per workspace
- One user can own up to 10 workspaces and be a member of unlimited workspaces via invitations
- Each workspace has role-based access control (RBAC)

**Workspace Roles:**

| Role | Permissions |
|------|------------|
| **Owner** | Full access — billing, members, contexts, memories, settings |
| **Admin** | Manage members and shared contexts, read/write memories |
| **Member** | Read/write memories in assigned contexts |
| **Viewer** | Read-only access to assigned contexts |

## Context

A **Context** is a namespace for organizing memories — like a folder for your AI's knowledge.

- Each context is isolated: searches only return memories within that context
- Separate contexts for separate purposes (e.g., `my-project`, `team-wiki`, `learning-notes`)
- Keeping contexts focused improves search accuracy
- Created via Web UI or MCP `create_context` tool

**Privacy levels:**

| Setting | Access |
|---------|--------|
| **Private** (`is_private=true`) | Only the creator can access |
| **Shared** (`is_private=false`) | All workspace members can access |

**Naming rules:** lowercase alphanumeric + hyphens/underscores only (`^[a-z0-9_-]+$`)

## Memory

A **Memory** is a single piece of knowledge stored in Kagura Memory Cloud. Each memory uses a **3-layer architecture** optimized for search and retrieval:

```
┌─────────────────────────────────────────────────┐
│ Layer 1: Summary (50-500 chars)                 │
│   → Embedded as vector for semantic search      │
│   → Write the conclusion, not the process       │
│   ✅ "JWT expiry caused 401. Fixed with         │
│      refresh token rotation."                   │
│   ❌ "Discussed auth errors in meeting."        │
├─────────────────────────────────────────────────┤
│ Layer 2: Context Summary (optional)             │
│   → Why this memory matters                     │
│   → How and when to use it                      │
├─────────────────────────────────────────────────┤
│ Layer 3: Content + Details (full data)          │
│   → Complete code, documentation, procedures    │
│   → Structured metadata as JSON                 │
└─────────────────────────────────────────────────┘
```

**Key attributes:**

- **type** — `code`, `note`, `decision`, `bug-fix`, `feature`, `learning`, etc.
- **importance** — `0.0-1.0` (critical=0.9+, useful=0.6-0.8, reference=0.3-0.5)
- **tags** — For categorization and filtering (e.g., `["python", "auth", "jwt"]`)
- **scope** — `working` (short-term) or `persistent` (long-term)

## Hybrid Search

When you search with `recall()`, Kagura uses **Hybrid Search** combining two approaches:

```
┌──────────────────────┐    ┌──────────────────────┐
│  Semantic Search     │    │  Full-Text Search    │
│  (60% weight)        │    │  (40% weight)        │
│                      │    │                      │
│  OpenAI Embedding    │    │  Qdrant BM25         │
│  Vector similarity   │    │  Keyword matching    │
│  "What does it mean?"│    │  "What words match?" │
└──────────┬───────────┘    └──────────┬───────────┘
           │                           │
           └─────────┬─────────────────┘
                     ▼
            ┌────────────────┐
            │  Merged Results │
            │  (RRF fusion)   │
            └────────┬───────┘
                     ▼
            ┌────────────────┐
            │  Optional       │
            │  AI Reranking   │
            └────────────────┘
```

**Tips for better search:**
- Use the **HyDE technique**: generate a hypothetical answer, then search with it
- Use **tag filters** for precision: `filters={"tags": ["python"]}`
- Use **importance filters**: `filters={"importance": {"gte": 0.8}}`

## Neural Memory

**Neural Memory** creates automatic relationships between memories using brain-inspired algorithms.

### Hebbian Learning

> "Neurons that fire together, wire together."

When memories are accessed together (e.g., recalled in the same session), a connection (edge) is created or strengthened between them. Edge weights range from 0.0 to 3.0.

### Activation Spreading

The `explore()` tool uses graph traversal to discover related memories:

```
  Seed Memory ──(0.15)──→ Related Memory A
       │                        │
    (0.08)                   (0.12)
       │                        │
       ▼                        ▼
  Related Memory B        Related Memory C
```

Starting from a seed memory, activation spreads outward through the graph, returning memories ranked by connection strength.

## Sleep Maintenance

**Sleep Maintenance** is a nightly background cycle that pays down memory debt asynchronously. Write paths optimize for ingest speed; over time this leaves near-duplicates, stale importance values, and graph gaps. Sleep runs per context and executes six phases in order:

1. **Edge Discovery** — find missing edges between related memories via medium-similarity search + optional LLM judgment
2. **Dedup / Merge** — cluster high-similarity memories and merge duplicates
3. **Importance Re-eval** — adjust importance via LLM scoring with EMA smoothing
4. **Consolidation** — promote / keep / archive working memories (replaces the legacy rule-only consolidation)
5. **Reindex** — re-embed memories modified by earlier phases so Qdrant stays in sync with PostgreSQL
6. **Report** — aggregate per-phase results and the action audit log

Each context has a `sleep_mode` setting (`full`, `edges_only`, or `skip`) that controls which phases run. Every action is recorded in `sleep_actions` and can be reversed via `rollback_sleep_run`.

See [Sleep Maintenance](sleep-maintenance.md) for the complete reference.

## MCP Tools

Kagura Memory Cloud exposes 26 tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/):

| Tool | Description | Read-only |
|------|------------|-----------|
| `remember` | Store a new memory | No |
| `recall` | Search memories (Hybrid Search) | Yes |
| `reference` | Get full details of a memory (all 3 layers) | Yes |
| `update_memory` | Update an existing memory or upsert by external ID | No |
| `forget` | Soft-delete a memory (30-day retention) | No |
| `explore` | Discover related memories via Neural Memory graph | Yes |
| `list_edges` | List edges connected to a memory | Yes |
| `create_edge` | Create an edge between two memories | No |
| `update_edge` | Update edge weight or type | No |
| `delete_edge` | Delete an edge between two memories | No |
| `get_context_info` | Get context metadata and usage guidelines | Yes |
| `list_contexts` | List available contexts in workspace | Yes |
| `create_context` | Create a new context (owner/admin only) | No |
| `update_context` | Update context settings | No |
| `delete_context` | Delete a context and all its memories | No |
| `merge_contexts` | Merge memories from source into target context | No |
| `update_search_config` | Tune hybrid search weights and reranker per context | No |
| `get_usage` | Quota and usage queries for the current workspace | Yes |
| `get_sleep_history` | List recent Sleep Maintenance runs for a context | Yes |
| `get_sleep_report` | Fetch a Sleep run's full report and action audit log | Yes |
| `rollback_sleep_run` | Reverse every recorded action of a Sleep run | No |
| `setup_resource` | Register or update a Resource (schema + indexer config) | No |
| `ingest_events` | Append versioned events to a Resource | No |
| `get_resource_schema` | Fetch the schema for a Resource | Yes |
| `get_resource_impact` | Preview how a Resource re-index would affect memories | Yes |
| `list_resource_tokens` | List Resource Tokens scoped to the caller | Yes |

**Typical workflow:**

```
list_contexts()           → Discover available contexts
  ↓
get_context_info(id)      → Load context guidelines
  ↓
recall(query)             → Search for relevant memories
  ↓
reference(memory_id)      → Get full details
  ↓
remember(summary, content) → Store new knowledge
  ↓
explore(memory_id)        → Find related memories
```

## Data Isolation

All data is isolated using a **3-level filtering** system:

```
Level 1: Workspace ID  → Team/organization boundary
Level 2: Context ID    → Project/topic boundary
Level 3: User ID       → Personal boundary (skipped for shared contexts)
```

This ensures that:
- Users in different workspaces can never see each other's data
- Memories in different contexts are never mixed in search results
- Private context memories are only visible to their creator

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Clients                               │
│  Claude Desktop / ChatGPT / Any MCP Client / Web UI     │
└───────────────────────┬─────────────────────────────────┘
                        │ MCP / REST API
┌───────────────────────▼─────────────────────────────────┐
│                 FastAPI Backend                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────┐ │
│  │ MCP      │ │ REST API │ │ OAuth2   │ │ Rate Limit │ │
│  │ Server   │ │ Routes   │ │ Server   │ │ Middleware │ │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────────────┘ │
│       └─────────────┼───────────┘                        │
│              ┌──────▼──────┐                             │
│              │   Services  │ (Memory, Context, Quota)    │
│              └──────┬──────┘                             │
│       ┌─────────────┼─────────────┐                      │
│  ┌────▼────┐  ┌─────▼─────┐ ┌────▼────┐                │
│  │PostgreSQL│  │  Qdrant   │ │  Redis  │                │
│  │(metadata)│  │ (vectors) │ │ (cache) │                │
│  └─────────┘  └───────────┘ └─────────┘                │
└─────────────────────────────────────────────────────────┘
```

For detailed architecture documentation, see [architecture.md](./architecture.md).
