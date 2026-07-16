# Architecture Overview

Kagura Memory Cloud is built with a modern, scalable architecture designed for production use. It implements the **LLM Knowledge Base** pattern (Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)) at team scale — see [LLM Knowledge Base — 5-Layer Mapping](#llm-knowledge-base--5-layer-mapping) below.

## LLM Knowledge Base — 5-Layer Mapping

Karpathy's pattern describes any "living knowledge base" as 5 layers. Kagura's implementation:

| Layer | Karpathy's intent | Kagura implementation | Code location |
|---|---|---|---|
| **Ingest** | Raw source intake | REST `/api/v1/memory`, MCP `remember`, R2 file storage (binary blobs), resource tokens for external feeds | `backend/src/api/routes/memory.py`, `backend/src/api/routes/files.py`, `backend/src/services/resource_indexer.py` |
| **Compile** | LLM rewrites raw → structured wiki pages | **MCP-as-compile-API**: chat agent emits structured `remember(summary, content, type, tags, importance)` per fact (continuous micro-compile). Sleep Maintenance phases (consolidation, deduplication, edge formation) handle batch consolidation. | `backend/src/mcp_server/tools/memory.py`, `backend/src/services/sleep/orchestrator.py` |
| **Index** | Page-level TOC for navigation | **Triple-index, all auto-maintained**: BM25 (keyword) + Qdrant vector (semantic) + Hebbian graph (relational). Context-level TOC via `list_contexts` / `get_context_info`. | `backend/src/services/search_service.py`, `backend/src/services/graph_service.py`, Qdrant collection |
| **Query** | Read pages directly, retrieve when needed | Hybrid Search (60% semantic + 40% BM25), AI Reranker (Voyage / Cohere / Self-hosted (Ollama, vLLM)), `explore` graph traversal for serendipity | `backend/src/services/search_service.py`, `backend/src/services/reranker_service.py` |
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
│   MCP Server (HTTP)  │          REST API (FastAPI)          │
│  - 60 MCP Tools      │  - Memory CRUD                       │
│    (memory / agent   │  - OAuth2 endpoints                  │
│     substrate/control│  - API Key + Agent management        │
│     / edges / context│  - Agent state + feedback lanes      │
│     / files / analysis│ - Resource Ingest API               │
│     / resources /    │  - Admin: sleep-reports, neural cfg  │
│     sleep / usage)   │                                      │
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
│  AgentRegistry │ AgentBinding │ AgentBootstrap              │
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

### Edge Origins

Neural Memory edges carry an `origin` discriminator (`neural_memory_edges.origin`, `VARCHAR(20)`, CHECK constraint `valid_edge_origin`) that controls their lifetime. Three values exist:

| | **`hebbian`** | **`semantic`** | **`declared`** |
|---|---|---|---|
| Source | runtime co-activation in `HebbianLearner` (write side of `recall()` — see Issue #120) | sleep `edge_discovery` (cosine sim ≥ 0.5) | user-asserted via MCP `create_edge` |
| Weight rule | see [§ Formulas](#formulas) — `Δw_ij ← η · (a_i · C_i) · (a_j · C_j) − λ · w_ij`, clamped 0.0–3.0 | `cosine_sim` at creation (0.5–1.0); never modified by decay | user-supplied (default 1.0) |
| Decays | yes — nightly `DecayManager.bulk_decay_weights(only_origin='hebbian')` | no | no |
| Pruned | yes — `DecayManager.prune_weak_edges(only_origin='hebbian')` below `prune_threshold` | no | no |
| Re-verified | n/a | monthly `semantic_edge_reverify` cron drops rows whose endpoint memory has been soft-deleted | n/a |

**Sticky-upsert invariant**: the `create_or_update_edge` upsert path is one-way — a runtime Hebbian co-recall write (which always carries `origin='hebbian'`) **cannot demote** an existing `semantic` or `declared` edge back to `hebbian`. The origin is set by the caller, not derived inside the upsert, so promotion happens only when a non-Hebbian writer chooses a non-Hebbian origin. At runtime, non-Hebbian origins are produced by exactly two writers: the Sleep maintenance `edge_discovery` phase (`backend/src/services/sleep/edge_discovery.py`) writes `origin='semantic'`, and the MCP `create_edge` tool (`backend/src/mcp_server/tools/edge.py`) writes `origin='declared'`. (The one-shot `backend/scripts/backfill_semantic_edges.py` migration script also writes `origin='semantic'` but is not part of the runtime write path.) Runtime Hebbian co-recall never picks a non-Hebbian origin, so it can only leave existing non-Hebbian edges alone — the invariant is "no demotion", not "bidirectional promotion".

**MCP duplicate contract (#1321)**: the sticky-upsert above protects `origin`, but not a declared edge's *values* — so the MCP `create_edge` handler adds its own layer. On an existing (source, target) pair: an auto edge (`hebbian`/`semantic`) gets the asserted values and the response reports `operation: "updated"` with a `previous` pre-image (a hebbian row is promoted to `origin='declared'`; a semantic row keeps `origin='semantic'` per the sticky-origin CASE — the response's `edge.origin` field exposes which happened); a `declared` edge with identical values is a no-op (`operation: "unchanged"`, keeps retries idempotent); a `declared` edge with different values is rejected with `edge_exists` unless the caller passes `overwrite=true` — declared links are provenance and are never silently clobbered. This contract is MCP-only: the repository upsert stays unconditional because SDK/worker replay paths depend on it.

**Background jobs maintaining `neural_memory_edges`**:

- **`DecayManager`** (nightly) — applies exponential decay and prune-below-threshold to `origin='hebbian'` rows only. `semantic` and `declared` rows are skipped.
- **`semantic_edge_reverify`** (monthly, 04:15 UTC on the 1st) — walks `origin='semantic'` rows and deletes any whose endpoint memory has been soft-deleted. Cost is O(edges), not O(memory²), so it stays cheap as contexts grow.

#### Formulas

**Hebbian update** (`backend/src/neural/hebbian.py` — applied per `recall()` for co-activated pairs that pass the semantic-gating threshold `min_similarity_for_edge`):

```
Δw_ij ← η · (a_i · C_i) · (a_j · C_j) − λ · w_ij
```

- `η` — learning rate (`config.learning_rate`)
- `a_i, a_j` — activation strengths of the co-activated nodes
- `C_i, C_j` — confidence / trust scores of each node (poisoning defense — low-trust nodes contribute less)
- `λ` — L2 decay coefficient applied per update (prevents weight explosion; separate from the nightly time-based decay below)
- `w_ij` — current edge weight, clamped to `[0.0, 3.0]`

**Time-based decay** (`backend/src/neural/decay.py` — applied nightly to `origin='hebbian'` only):

```
w_ij(t + Δt) = w_ij(t) · exp(−decay_rate · Δt)
```

After decay, any row with `w_ij < prune_threshold` is removed by `prune_weak_edges`. `semantic` and `declared` rows are decay-exempt by construction (`only_origin='hebbian'` filter), so they survive arbitrarily long Δt — which is the load-bearing property for content-based edge survival as N grows.

#### Edge lifecycle

```
hebbian
  recall() co-activation                 reinforced by next co-recall
  + semantic gating passed         ┌─► weight rises (Δw_ij update)
       │                           │
       ▼                           │
  create_or_update_edge ───────────┘
  (origin='hebbian')               not reinforced
       │                          ┌─► nightly DecayManager
       ▼                          │   weight *= exp(−decay_rate · Δt)
  edge exists ────────────────────┘
       │                          weight < prune_threshold
       └──────────────────────────► prune_weak_edges() removes row

semantic
  sleep edge_discovery
  + cosine_sim ≥ min_similarity_for_edge
       │
       ▼
  create_or_update_edge ──► edge exists (origin='semantic')
  (origin='semantic',           │
   weight=cosine_sim)           │   endpoint memory soft-deleted
                                └─► monthly semantic_edge_reverify removes row
                                    (never touched by DecayManager)

declared
  MCP create_edge tool
       │
       ▼
  create_or_update_edge ──► edge exists (origin='declared')
  (origin='declared')           │
                                │   MCP delete_edge / update_edge
                                └─► row removed or weight updated
                                    (never touched by DecayManager or reverify)
```

**Sticky-upsert detail**: when a co-recall triggers `create_or_update_edge` on a pair that already has a `semantic` or `declared` edge, the existing row's `origin` is **preserved** (no demotion). The weight may still be modified by the Hebbian formula, but the row will continue to be exempt from the nightly decay loop.

For the reader-friendly framing (hippocampus / cortex analogy and the `1/N²` motivation), see [Neural Memory § Edge Origins](concepts.md#edge-origins) in the concepts doc. For the migration history, deploy runbook, and backfill procedure, see [`docs/operations/semantic-edge-rollout.md`](operations/semantic-edge-rollout.md). For the `Memory.scope` (`working` / `persistent`) lifecycle — which is **orthogonal** to edge origin and managed by Sleep Consolidation — see [Sleep Maintenance](sleep-maintenance.md).

> **Note**: the schema documented above reflects the shipped state as of v0.16.x. A richer two-axis provenance schema (relation × origin × frozen flag) is in design as RFC #84 and may extend this taxonomy in a future release.

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

## Agent Memory Substrate Lanes

Beyond the human-facing knowledge base, Kagura is an **agent memory substrate** (epic #885, v0.24.0 / v0.25.0). An autonomous agent loop needs three things a semantic memory store must *not* provide directly: durable exact-match scratch state, an explicit usefulness signal, and a provenance/trust boundary on what may influence behaviour. These ship as **dedicated lanes that sit beside `memories`** — deliberately separate tables so they are **structurally excluded from `recall()`** rather than filtered out at query time. For the reader-facing concept model (the four primitives — delivery / trust / state / feedback — and the "primitives, not new types" decision), see [Concepts › Agent Memory Substrate](concepts.md#agent-memory-substrate).

### Lane separation

| Lane | Table | In `recall()`? | Why a separate lane |
|---|---|---|---|
| Knowledge | `memories` | Yes (Hybrid Search) | The searchable corpus |
| Agent state | `agent_states` | **No** | Exact-match key→value scratch (task, plan step, cursor); semantic search over it is meaningless and would poison results |
| Feedback | `retrieval_feedback` | **No** | Append-only signal *about* recall quality; embedding it would create a feedback-of-feedback loop |

`agent_states` is read by `set_state` / `get_state` (MCP) and `/api/v1/contexts/{id}/state*` (REST); `retrieval_feedback` by `feedback` (MCP) and `POST /api/v1/contexts/{id}/feedback` (REST). Both FK to `contexts` with `ON DELETE CASCADE`, so an agent's state and feedback are erased with their context (GDPR/APPI erasure follows automatically).

### Trust boundary (provenance & behaviour-influencing reads)

The substrate adds a server-enforced trust boundary so that **untrusted external content cannot be silently treated as instructions** (indirect prompt injection — [OWASP LLM01 / LLM03](https://owasp.org/www-project-top-10-for-large-language-model-applications/)):

- **`memories.source_type`** (`VARCHAR(20)`, NOT NULL, CHECK) — **server-stamped** provenance, never client-supplied: `manual` / `file` / `url` / `vault` / `api` / `connector`. Backfilled to `manual` for pre-existing rows by migration `e35_887`.
- **`contexts.trust_tier`** (`VARCHAR(20)`, NOT NULL, default `trusted`, CHECK) — `trusted` or `external`. Connector-fed contexts are `external`.
- **Trust filter** — `recall(filters={"trust_tier": "trusted"})` excludes memories whose context is `external`-tier **and** any `source_type="connector"` memory (defence-in-depth). Applied inside `MemoryService` recall (`backend/src/services/memory_service.py`). Opt-in; intended for any read whose results are fed back to the model as context.

### Feedback eval gate — no self-update loop

The feedback signal is the prerequisite for a future Eval→Skill self-update loop, but **closing that loop is gated**: no mechanism that promotes, demotes, re-ranks, or rewrites memories from feedback ships before the golden retrieval eval gate (#344) is green. The deterministic layer of the eval harness ([`backend/tests/eval/`](../backend/tests/eval/README.md) — leakage check, corpus-schema, stratification, metrics) runs in normal CI; the live P@5 / MRR measurement (`make eval-retrieval`) is the numeric regression gate, not yet wired into CI (#336). Full policy: [Retrieval Feedback & Eval Gate](eval/retrieval-feedback-and-eval-gate.md).

### Connector canonical chat schema

Connectors provisioned via `setup_connector` (#910 / #911) seed a canonical chat **resource schema** at registration: a single `text` field (fulltext+vector indexed) holding the ai-worker's per-message LLM summary. Lineage (`source_uri` / memory details) stays in `event_metadata`, not the payload. Defined in `ConnectorProvisioningService` (`backend/src/services/connector_provisioning.py`).

## Agent Memory & Context Control Plane (v0.49.0 preview)

The control plane extends workspace RBAC rather than creating a second principal system:

1. `agents` registers a workload inside one workspace. The row is a resource, not a credential.
2. An owner-provisioned member key may point to the agent through nullable `api_keys.agent_id`. Verification rejects keys for `suspended` or `retired` agents.
3. `agent_context_bindings` narrows the underlying member/RBAC decision. In `enforce` mode, only bound contexts survive the intersection; in `shadow` mode the legacy permission result remains active while violations are observed.
4. `get_agent_bootstrap` resolves the agent and binding, then composes existing context/pinned/recall/upcoming/state services. It does not introduce a parallel retrieval or ranking path.

Registry, context-level bindings, bootstrap, transport correlation, and the append-only `memory_access_events` audit foundation are implemented. REST middleware and the MCP authentication seam parse W3C `traceparent`/baggage into a request-local correlation context; credential-bound identity has precedence, and missing trace/span IDs are generated server-side. This is observability plumbing, not an authorization input or server-side span exporter. Per-memory type/source filters are enforced on the memory-read lanes (recall, reference, forget, explore, load_pinned, upcoming) for enforce-mode agents as of [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299) — `null` = all, `[]` = deny-all; shadow records `would_deny` without filtering. The audit writer covers bootstrap, load-pinned, feedback, recall, reference, remember, update, and forget with binding deny persistence.

## Database Design

### PostgreSQL Tables

Tables are grouped by domain. The authoritative list lives in `backend/src/models/`.

**Identity & access**
- **users** — User accounts
- **api_keys** — API key management (SHA256 hashed); optional `agent_id` binds an owner-provisioned member key to a registered agent
- **external_api_keys** — OpenAI/Cohere keys (Fernet encrypted)
- **oauth_clients** / **oauth_authorization_codes** / **oauth_tokens** — OAuth2 server
- **audit_logs** — Security-relevant audit trail
- **agents** — Workspace-scoped agent registry with lifecycle (`active | suspended | retired`) and binding enforcement mode (`shadow | enforce`)
- **agent_context_bindings** — Purely subtractive per-agent context read/write/default policy; `allowed_memory_types` / `allowed_source_types` enforce per-memory read filtering (`null` = all, `[]` = deny-all) as of #1299

**Workspaces & contexts** (top-level tenancy)
- **workspaces** — Top-level organizational unit (team / project owner)
- **workspace_members** — Role assignments (Owner / Admin / Member / Viewer)
- **workspace_invitations** — Pending invitations
- **workspace_addons** — Per-workspace addon entitlements
- **contexts** — Memory namespaces scoped to a workspace
- **context_members** — Per-context access (private / shared)
- **context_search_configs** — Per-context hybrid search weights and reranker tuning

**Memories & graph**
- **memories** — 3-layer memory storage. Carries server-stamped `source_type` (provenance) and `delivery_mode` (`on_recall` / `always` / `on_trigger`)
- **attachments** — Legacy small-file rows (≤5 MB) stored inline as PostgreSQL `BYTEA`. The public `/api/v1/attachments/*` routes are deprecated and return `410 Gone`; new uploads use `file_objects` in R2
- **neural_memory_edges** — Primary Hebbian edge storage (workspace + context scoped)
- **graph_memory** — Legacy NetworkX JSON (read paths still reference it; new writes go to `neural_memory_edges`)

**Agent memory substrate** (epic #885, v0.24.0 / v0.25.0 — see [Agent Memory Substrate Lanes](#agent-memory-substrate-lanes))
- **agent_states** — Per-context key→value JSON scratch state with optional TTL (`expires_at`); unique on `(context_id, key)`. Structurally excluded from `recall()`
- **retrieval_feedback** — Append-only `helpful` signal per `(context_id, memory_id)`, attributed to `user_id`. Structurally excluded from `recall()`
- (`contexts.trust_tier` + `memories.source_type` add the provenance/trust boundary — see column notes above)

**Resource ingest** (v0.12.0 Resource Foundation)
- **resources** — Normalized Resource entity (UUID PK); satellite tables FK via `resource_pk`
- **resource_events** — Append-only event log (upsert / delete events)
- **resource_schemas** — Versioned schemas declared per Resource
- **indexer_state** — Per-(resource, context) indexer cursor + error metrics
- **resource_tokens** — Per-Resource ingest tokens (workspace-scoped)

Since v0.48.0, REST and MCP batch ingest are thin adapters over `ResourceIngestService`. Quota, identity resolution, UTF-8 byte-size validation, per-event SAVEPOINT handling, partial-success semantics, commit behavior, and post-commit scheduling therefore have one implementation on both surfaces.

**File storage (R2 object storage)** (Issue #485 — see [Object Storage (R2)](#object-storage-r2))
- **file_objects** — One row per uploaded file held in Cloudflare R2. `status` ∈ `reserved | uploaded | failed` (`CHECK`); `storage_backend` is `r2`-only (`CHECK valid_file_storage_backend`); `storage_key` = `{workspace_id}/{sha256[:2]}/{sha256}`. A partial-unique index dedups *active* files per `(workspace_id, lower(sha256))` (excludes soft-deleted and `failed` rows). **This — not `attachments` — is the R2-backed path**; `attachments` is inline Postgres BYTEA.
- **workspace_storage_usage** — Denormalized per-workspace `(used_bytes, file_count)` counter, maintained atomically with `file_objects` inserts/soft-deletes so the quota path reads one row instead of an online `SUM`.

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

### Object Storage (R2)

Binary file uploads are stored in **Cloudflare R2**, not in Postgres. R2 is the
**only** object-storage backend — there is no `STORAGE_BACKEND` switch, and the
`file_objects.storage_backend` column is pinned to `r2` by a `CHECK`. R2 access is
configured via the `R2_*` environment variables. The canonical schema lives in
`backend/src/models/file_objects.py` + alembic `e03_485_file_objects`; this section
is the conceptual overview (Issue #485). Bucket keys are
`{workspace_id}/{sha256[:2]}/{sha256}` — the `sha256[:2]` sub-prefix spreads objects
to avoid an R2 hot partition.

**Three-step presigned upload** (`backend/src/services/file_storage_service.py`,
route `backend/src/api/routes/files.py`). The server never streams the bytes — the
client PUTs directly to R2, so this flow is **not** usable from the MCP protocol alone:

1. **reserve** — `reserve_upload` validates size / sha256 / content_type, reserves
   workspace quota, inserts a row with `status='reserved'`, and returns a short-lived
   **presigned PUT URL**. `storage_key` may be `NULL` in flight (the
   `valid_file_storage_shape` CHECK exempts `reserved` rows).
2. **PUT** — the client uploads the bytes directly to R2 via the presigned URL.
3. **confirm** — `confirm_upload` verifies the object with `head_object`, then
   transitions `reserved → uploaded`. Idempotent on retry (re-confirming an already
   `uploaded` row with a matching sha256 is a no-op).

**Integrity** — when `R2_CHECKSUM_BINDING_ENABLED` is on (Issues #556 / #574), the
presigned URL is bound to the expected sha256 storage-side so R2 itself rejects a
mismatched body; the caller's claimed-sha256 check is defense-in-depth.

**Lifecycle sweepers** (`backend/src/tasks/file_tasks.py`):
- **Orphan sweeper** (`sweep_orphan_files`, 15-minute interval) — `reserved` rows past
  `expires_at + 1h` are marked `status='failed'` (which frees the active-dedup index),
  their reserved quota is released, and any dangling R2 object is best-effort deleted.
- **Soft-delete GC** (`sweep_soft_deleted_files`, nightly, Issue #552) — hard-deletes
  rows that are `status='uploaded' AND deleted_at IS NOT NULL` and older than the 7-day
  retention window (`_GC_RETENTION_SECONDS`), along with their R2 binaries. Each sweep
  loads candidates against a dedicated partial index.

**Quota** — `backend/src/services/storage_quota_service.py` enforces the per-workspace
byte quota by reading the single `workspace_storage_usage` row (no online `SUM`).

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

**Agent-bound credentials** add a subtractive layer after the existing RBAC decision. `enforce` mode intersects access with `agent_context_bindings`; `shadow` mode records the would-deny result without narrowing access. Requests made with keys that have no `agent_id` are unchanged.

All checks funnel through `PermissionService` in `backend/src/services/permission_service.py`; clients never supply a raw `workspace_id` without server-side verification.

### Encryption

- **API Keys**: SHA256 hash storage
- **External API Keys**: Fernet symmetric encryption
- **JWT Tokens**: HS256 signing (1 hour expiration)
- **OAuth2 Secrets**: SHA256 hash storage

### Signup Gate

The admin-configurable signup gate (`backend/src/services/signup_gate_service.py`) sits in front of the OAuth callback's user-creation step. It applies **uniformly to both GitHub and Google** (since #655 — the original #358 Phase 1 design trusted Google's Consent Screen test-user list, but that list is only strictly enforced for sensitive scopes and is click-through for basic profile scopes).

**Match key**: `(provider, subject_id)` in `signup_allowlist`. `subject_id` is the immutable IdP identity (GitHub numeric ID for github rows, OIDC `sub` claim for google rows). Email is never used for matching — that would re-open email-change attacks at the IdP.

**Modes** (singleton `signup_gate_config.mode`):
- `manual`: signup allowed iff `(provider, subject_id)` is on the allowlist with `state='active'`.
- `github_sponsors` / `both`: Phase 2 (NotImplemented for GitHub today; for Google, falls back to `manual` since sponsorship is GitHub-specific).

**Blocked signup observability**: every blocked attempt writes to `audit_logs` (action=`signup_blocked`) with the email HMAC'd (never plaintext), plus IP / User-Agent for triage. The blocked redirect to `/signup-blocked?provider=<p>&sub=<first8>` lets the frontend render an admin-contact prompt without leaking the full identity.

**Admin API**: `POST /api/v1/admin/signup-gate/allowlist` accepts either `{github_username}` (legacy GitHub shape) or `{provider: "google", email}`. Google adds resolve the OIDC `sub` from a pre-existing `users` row — the invitee must complete Google OAuth at least once before they can be allowlisted (pre-OAuth invitation by email is a Phase 2 follow-up).

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
| **Database** | PostgreSQL | 18+ |
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
