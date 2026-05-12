# Core Concepts

This document explains the fundamental concepts of Kagura Memory Cloud.

> **Mental model**: Kagura is a team-scale **LLM Knowledge Base** following the 5-layer pattern from [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — Ingest, Compile, Index, Query, Enhance. The concepts below map onto these layers: a Workspace owns Contexts (Index unit), each Context holds Memories (Compile unit, 3-layer schema), and the Neural Memory graph + Sleep Maintenance drive Enhance. See [Architecture](architecture.md#llm-knowledge-base--5-layer-mapping) for the full mapping.

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

Kagura Memory Cloud exposes 37 tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), grouped into 9 categories:

| Category | Tools | Purpose |
|----------|-------|---------|
| Memory | 6 | `remember`, `recall`, `reference`, `update_memory`, `forget`, `explore` — store / search / discover memories |
| Neural Edges | 4 | `list_edges`, `create_edge`, `update_edge`, `delete_edge` — manage the Hebbian graph manually |
| Contexts | 7 | `get_context_info`, `list_contexts`, `create_context`, `update_context`, `delete_context`, `merge_contexts`, `update_search_config` |
| Tags | 1 | `list_tags` — tag vocabulary discovery for alignment before `remember`/`recall` |
| Files / R2 | 5 | `init_file_upload`, `complete_file_upload`, `list_files`, `get_file_download_url`, `delete_file` — binary attachments via R2 |
| Analyses (Broadlistening) | 5 | `analyze_context`, `list_analyses`, `get_analysis`, `get_active_analysis`, `get_cluster` — large-scale qualitative clustering (Owner + Pro plan) |
| Resources | 5 | `setup_resource`, `list_resource_tokens`, `ingest_events`, `get_resource_impact`, `get_resource_schema` — external data ingestion |
| Sleep Maintenance | 3 | `get_sleep_history`, `get_sleep_report`, `rollback_sleep_run` — background consolidation observability |
| Usage | 1 | `get_usage` — workspace quota and usage queries |

See [README › MCP Tools](../README.md#mcp-tools) for the full per-tool table with descriptions and required roles.

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
