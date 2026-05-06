# Architecture Overview

Kagura Memory Cloud is built with a modern, scalable architecture designed for production use. It implements the **LLM Knowledge Base** pattern (Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)) at team scale — see [LLM Knowledge Base — 5-Layer Mapping](#llm-knowledge-base--5-layer-mapping) below.

## LLM Knowledge Base — 5-Layer Mapping

Karpathy's pattern describes any "living knowledge base" as 5 layers. Kagura's implementation:

| Layer | Karpathy's intent | Kagura implementation | Code location |
|---|---|---|---|
| **Ingest** | Raw source intake | REST `/api/v1/memory`, MCP `remember`, R2 file storage (binary blobs), resource tokens for external feeds | `backend/src/api/routes/memory.py`, `backend/src/api/routes/files.py`, `backend/src/services/resource_indexer.py` |
| **Compile** | LLM rewrites raw → structured wiki pages | **MCP-as-compile-API**: chat agent emits structured `remember(summary, content, type, tags, importance)` per fact (continuous micro-compile). Sleep Maintenance phases (consolidation, deduplication, edge formation) handle batch consolidation. | `backend/src/mcp_server/tools/memory.py`, `backend/src/services/sleep/orchestrator.py` |
| **Index** | Page-level TOC for navigation | **Triple-index, all auto-maintained**: BM25 (keyword) + Qdrant vector (semantic) + Hebbian graph (relational). Context-level TOC via `list_contexts` / `get_context_info`. | `backend/src/services/search_service.py`, `backend/src/services/graph_service.py`, Qdrant collection |
| **Query** | Read pages directly, retrieve when needed | Hybrid Search (60% semantic + 40% BM25), AI Reranker (Voyage / Cohere / Ollama), `explore` graph traversal for serendipity | `backend/src/services/search_service.py`, `backend/src/services/reranker_service.py` |
| **Enhance** | Compounding loop — answers feed back as new pages | **Hebbian learning**: every `recall()` strengthens edges between co-retrieved memories (zero LLM cost background graph evolution). Sleep Maintenance phases reorganize periodically. **Explicit write-back**: `/kagura-memory:session-summary` skill encourages user/agent to file synthesized answers as new memories — opt-in to avoid noise. | `backend/src/neural/`, `backend/src/services/sleep/`, `claude-skills/session-summary.md` |

**Difference from Karpathy's pattern**: Kagura targets team/org scale (multi-tenant DB) and treats each `remember()` as a micro-compile (continuous), where the LLM Wiki pattern targets personal scale (~100 markdown pages) with batch wiki rewrites. The compile interface is schema-enforced (Pydantic `MemoryCreate`) rather than free-form prose.

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Client Applications                      │
│  (Claude Desktop, Claude Code, ChatGPT, Web UI, Custom)     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    Authentication Layer                      │
│  OAuth2 (Google, GitHub) │ API Keys │ JWT Tokens            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────┬──────────────────────────────────────┐
│   MCP Server (SSE)   │          REST API (FastAPI)          │
│  - 26 MCP Tools      │  - Memory CRUD                       │
│    (memory / ctx /   │  - OAuth2 endpoints                  │
│     edge / search /  │  - API Key management                │
│     usage / sleep /  │  - Resource Ingest API               │
│     resource)        │  - Admin: sleep-reports, neural cfg  │
│  - Session Mgmt      │                                      │
│  - JSON-RPC          │                                      │
└──────────────────────┴──────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                      Service Layer                           │
│  MemoryService │ SearchService │ EmbeddingService           │
│  GraphService │ NeuralMemoryEngine │ WorkspaceService       │
│  PermissionService │ SleepService │ LLMService              │
│  ResourceIndexer │ ContextService │ QuotaService            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                     Data Access Layer                        │
│  SQLAlchemy async models (backend/src/models/)              │
│  + service-owned queries (no dedicated repository layer)    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────┬──────────────┬──────────────┬───────────────┐
│  PostgreSQL  │   Qdrant     │    Redis     │  External API │
│  - Memories  │  - Vectors   │  - Sessions  │  - OpenAI     │
│  - Workspace │  - BM25      │  - Cache     │  - Cohere     │
│  - Resources │  (single     │  - Rate Lmt  │  - Stripe     │
│  - Graph     │   collection)│              │               │
└──────────────┴──────────────┴──────────────┴───────────────┘
```

## 3-Layer Memory Architecture

### Layer 1: Summary
- **Purpose**: Quick search and retrieval
- **Content**: Concise summary (10-500 characters)
- **Storage**: PostgreSQL + Qdrant vector
- **Use case**: Initial search results

### Layer 2: Context Summary
- **Purpose**: Understanding context
- **Content**: Medium explanation (max 2000 characters)
- **Storage**: PostgreSQL
- **Use case**: Showing search result previews

### Layer 3: Details
- **Purpose**: Complete information
- **Content**: Full content + structured metadata (JSON)
- **Storage**: PostgreSQL
- **Use case**: Detailed view after selection

## Hybrid Search System

```
User Query
    ↓
┌─────────────────────────────┐
│  Query Processing           │
│  - Tokenization             │
│  - OpenAI Embedding         │
└─────────────────────────────┘
    ↓
┌──────────────┬──────────────┐
│  Semantic    │   BM25       │
│  (Vector)    │  (Keyword)   │
│  60% weight  │  40% weight  │
└──────────────┴──────────────┘
    ↓               ↓
Results A       Results B
    └───────┬───────┘
            ↓
    ┌───────────────┐
    │  Fusion       │
    │  (Weighted)   │
    └───────────────┘
            ↓
    ┌───────────────┐
    │  Cohere       │
    │  Reranking    │
    └───────────────┘
            ↓
      Final Results
```

### Search Weights
- **Semantic Search**: 60% (better for meaning)
- **BM25 Full-text**: 40% (better for exact keywords)
- **Reranking**: Cohere multilingual-v3.0

## Neural Memory Engine

### Hebbian Learning
Automatic relationship learning based on co-activation:

```python
# When memories A and B are accessed together
weight(A, B) += learning_rate * activation(A) * activation(B)
```

**Features**:
- Decays over time (forgetting curve)
- Strengthens with repeated co-access
- Creates knowledge graph automatically

### Activation Spreading
Graph-based exploration from seed memory:

```
Seed Memory
    ↓
  [depth=1]
    ├── Related Memory 1 (weight=0.9)
    ├── Related Memory 2 (weight=0.7)
    └── Related Memory 3 (weight=0.5)
         ↓
       [depth=2]
         ├── Sub-related 1 (weight=0.8)
         └── Sub-related 2 (weight=0.6)
```

**Parameters**:
- `depth`: Max hops (default: 2, max: 5)
- `min_weight`: Minimum edge weight (default: 0.5)
- `relation_types`: Filter by relation types

### Unified Scoring
Combines multiple signals:

```python
score = (
    0.4 * semantic_score +
    0.3 * graph_score +
    0.2 * temporal_score +
    0.1 * trust_score
)
```

## Database Design

### PostgreSQL Tables

Tables are grouped by domain. The authoritative list lives in `backend/src/models/`.

**Identity & access**
- **users** — User accounts
- **api_keys** — API key management (SHA256 hashed)
- **external_api_keys** — OpenAI/Cohere keys (Fernet encrypted)
- **oauth_clients** / **oauth_authorization_codes** / **oauth_tokens** — OAuth2 server
- **audit_logs** — Security-relevant audit trail

**Workspaces & contexts** (top-level tenancy)
- **workspaces** — Top-level organizational unit (team / project owner)
- **workspace_members** — Role assignments (Owner / Admin / Member / Viewer)
- **workspace_invitations** — Pending invitations
- **workspace_addons** — Per-workspace addon entitlements
- **contexts** — Memory namespaces scoped to a workspace
- **context_members** — Per-context access (private / shared)
- **context_search_configs** — Per-context hybrid search weights and reranker tuning

**Memories & graph**
- **memories** — 3-layer memory storage
- **attachments** — File attachments linked to memories
- **neural_memory_edges** — Primary Hebbian edge storage (workspace + context scoped)
- **graph_memory** — Legacy NetworkX JSON (read paths still reference it; new writes go to `neural_memory_edges`)

**Resource ingest** (v0.12.0 Resource Foundation)
- **resources** — Normalized Resource entity (UUID PK); satellite tables FK via `resource_pk`
- **resource_events** — Append-only event log (upsert / delete events)
- **resource_schemas** — Versioned schemas declared per Resource
- **indexer_state** — Per-(resource, context) indexer cursor + error metrics
- **resource_tokens** — Per-Resource ingest tokens (workspace-scoped)

**Plans, quotas & sleep**
- **user_plans** / **plan_changes** — Plan state and history
- **usage_stats** — Rate-limit and quota counters
- **sleep_reports** — Per-run Sleep Maintenance summaries
- **sleep_actions** — Reversible action audit log (used by `rollback_sleep_run`)

**Configuration**
- **neural_config** — Neural engine per-workspace config
- **config_overrides** — System-level config overrides
- **mcp_tool_descriptions** — Admin-editable MCP tool description overrides

### Qdrant Collections

**Design**: single shared collection per embedding model, scoped via payload filters.

- Default collection: `kagura_memories` (OpenAI `text-embedding-3-small`, 512 dims)
- Non-default models: `kagura_memories_{model_slug}_{dim}` (e.g. `kagura_memories_qwen3_embedding_8b_4096`)
- Name resolution: `get_collection_name(model, dimensions)` in `backend/src/db/qdrant.py`
- Isolation: every point carries `workspace_id` + `context_id` in its payload; searches add these as Qdrant filters
- Named vectors: `dense` (VectorParams) + `bm25` (SparseVectorParams) — anonymous vectors are rejected
- Distance metric: Cosine
- Tokenizer: Multilingual (auto Japanese support)

**Features**:
- Semantic vector search
- Full-text BM25 search (MatchText + sparse vector)
- Metadata filtering (workspace / context / tags / importance)

Prior "1 user = 1 collection" design (`kagura_user_{user_id}`) was replaced by the single-collection migration; no per-user collections remain in new deployments.

### Redis Storage

1. **Sessions**: Session-based authentication (7 days TTL)
2. **Cache**: Embedding cache, search results
3. **Rate Limiting**: Per-key request counters

## Security Architecture

### Authentication Flow

```
User → OAuth2 Login → Session Cookie → Access Token
                                    ↓
                            API Key Creation
                                    ↓
                        External API Access
```

### Authorization Model

Authorization is **workspace-scoped RBAC**. Every authenticated request is resolved to a `(user, workspace, context)` triple before any data access.

**System roles** (flag on `users`):
- **System admin**: Operator-level access (user management, system config, admin API endpoints)
- **Standard user**: Default — scoped by workspace membership

**Workspace roles** (per `workspace_members` row):
- **Owner**: Billing, members, contexts, memories, settings
- **Admin**: Manage members and shared contexts, read/write memories
- **Member**: Read/write memories in assigned contexts
- **Viewer**: Read-only access to assigned contexts

**Context-level privacy**:
- **Private** (`is_private=true`): Only the creator can access
- **Shared** (`is_private=false`): All workspace members with the appropriate role

All checks funnel through `PermissionService` in `backend/src/services/permission_service.py`; clients never supply a raw `workspace_id` without server-side verification.

### Encryption

- **API Keys**: SHA256 hash storage
- **External API Keys**: Fernet symmetric encryption
- **JWT Tokens**: HS256 signing (1 hour expiration)
- **OAuth2 Secrets**: SHA256 hash storage

## Scalability Considerations

### Horizontal Scaling
- **Backend**: Stateless FastAPI (scale with replicas)
- **Frontend**: Next.js static export (CDN-ready)
- **Database**: PostgreSQL connection pooling (asyncpg)
- **Redis**: Cluster mode support

### Performance Optimizations
- **Async I/O**: All database operations async
- **Connection Pooling**: PostgreSQL (20 max), Redis (10 max)
- **Caching**: Redis cache for embeddings (60 min TTL)
- **Batch Processing**: Background tasks with APScheduler

### Resource Usage (per instance)
- **CPU**: 2-4 cores (recommended)
- **Memory**: 4-8 GB (recommended)
- **Storage**: ~100 MB per 1000 memories (PostgreSQL + Qdrant)

## Deployment Architecture

```
┌──────────────────────────────────────┐
│         Load Balancer (GCP)          │
└──────────────────────────────────────┘
              ↓
┌──────────────────────────────────────┐
│      Backend Instances (Docker)      │
│  - your-domain.com (production)      │
│  - localhost:8080 (development)      │
└──────────────────────────────────────┘
              ↓
┌──────────────────────────────────────┐
│     Managed Services (GCP)           │
│  - Cloud SQL (PostgreSQL)            │
│  - Qdrant Cloud                      │
│  - Memorystore (Redis)               │
└──────────────────────────────────────┘
```

## Technology Stack

| Layer | Technology | Version |
|-------|-----------|---------|
| **Backend** | FastAPI | 0.115+ |
| **Database** | PostgreSQL | 15+ |
| **Vector DB** | Qdrant | 1.15+ |
| **Cache** | Redis | 7+ |
| **Frontend** | Next.js | 16 |
| **ORM** | SQLAlchemy | 2.0+ (async) |
| **Auth** | Authlib | 1.3+ |
| **AI** | OpenAI API | Latest |
| **Reranking** | Cohere API | Latest |
| **Graph** | NetworkX | 3.0+ |

## Monitoring & Observability

### Logging
- **Format**: Structured JSON logs (structlog)
- **Levels**: DEBUG, INFO, WARNING, ERROR
- **Destinations**: stdout (Docker), CloudWatch (prod)

### Metrics (Future)
- Request latency (p50, p95, p99)
- Error rates
- Database query performance
- Memory usage per user

### Health Checks
- `/health` - Basic health check
- `/api/v1/info` - Detailed system info
- Database connection status
- Qdrant connection status
- Redis connection status

## Future Enhancements

1. **Multi-region Deployment**: Global CDN + regional databases
2. **Real-time Collaboration**: WebSocket support for shared memories
3. **Advanced Analytics**: User behavior insights, usage patterns
4. **Custom Embeddings**: Fine-tuned models for specific domains
5. **GraphQL API**: Alternative to REST for complex queries
