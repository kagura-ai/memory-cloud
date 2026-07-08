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

## The Update Kernel (reinforce recency re-rank)

When a stored fact has been superseded — you remembered "the deploy target is
staging" last month and "the deploy target is prod" yesterday — the most
valuable thing a memory system can do is rank the **current** version above the
stale one. Kagura's measured answer to this is the **reinforce re-rank**
(#1048): a deterministic, LLM-free adjustment applied to the hybrid top-k
before results are returned.

Each candidate's score is multiplied by a factor bounded to
`[1 − max_boost, 1 + max_boost]` (default `max_boost = 0.15`), combining:

- **cold-start recency** — a day-scaled prior (`recency_tau_days`, default 14)
  that favors newer memories that haven't yet earned adoption signal;
- **adoption** — deliberate `reference()` calls (log-capped);
- **retrieval feedback** — explicit helpful/unhelpful verdicts.

Because the factor is bounded, semantic relevance always dominates: the
re-rank reorders close calls, it cannot resurrect an irrelevant result. It is
fail-safe — any error preserves the original hybrid ranking.

This mechanism is the system's best-measured behavior: a pre-registered,
placebo-controlled evaluation attributed a **+0.36 update-correctness lift
over vanilla RAG (BCa 95% [0.24, 0.50])** entirely to it. Since #1207,
**newly created contexts start with it enabled**. Contexts created before
#1207 keep their stored setting — under the old default that stored value is
typically `false`, so pre-existing contexts do **not** flip on upgrade;
enable them per context with `update_search_config`
(`reinforce_enabled: true`), guided by the graduation procedure in
`docs/eval/reinforce-rollout-gate.md` and the `reinforce_rerank_applied`
monitoring telemetry. (Rare legacy contexts that never got a config row
adopt the new default the first time the row is materialized.)

## Fact Succession (supersedes / contradicts)

When a fact changes, the worst thing a memory system can do is delete the old
version (the truth is gone) or keep returning it (the truth is buried). Since
#1208 Kagura models succession **non-destructively** with two typed relations:

- **`supersedes`** — `src` is the newer memory, `dst` the outdated one it
  replaces. A memory that is the dst of a live supersedes edge is
  **shadowed**: default `recall()` demotes it out of results, but it is never
  deleted — `recall(include_superseded=true)` returns it annotated with
  `superseded_by`, `explore()` still reaches it, and deleting the edge (or
  the superseding memory) restores full visibility. Create it by storing the
  updated fact with `remember(..., supersedes=<old_memory_id>)`, or
  explicitly with `create_edge(edge_type="supersedes")`.
- **`contradicts`** — two memories disagree and neither is known to be
  current. Contradiction **never hides** either side: both surface in recall
  annotated with the opposing memory ids. Resolving a contradiction is an
  arbitration decision (yours or a future judge's), not a deletion.

Sleep's dedup phase can also record succession instead of removing
duplicates: with `sleep_dedup_supersede_enabled` (default **off** —
update-by-removal remains the default), a judged merge creates a
`supersedes` edge (origin `semantic`) and leaves the loser
alive-but-shadowed. Neither type is LLM-emittable by edge discovery — the
sleep judge cannot invent supersession.

## Neural Memory

**Neural Memory** creates automatic relationships between memories using brain-inspired algorithms.

### Edge Origins

Neural Memory edges are tagged with one of three **origins**, each with different lifetime semantics. This mirrors the **complementary learning systems** view from cognitive neuroscience — short-lived episodic associations vs durable content-based associations vs user-pinned ones.

| | **Hebbian** (hippocampus-like) | **Semantic** (cortex-like) | **Declared** |
|---|---|---|---|
| Encodes | "fired together recently" — episodic co-activation | "about similar things" — content neighborhood | "I'm telling you these are related" — explicit user assertion |
| Created by | runtime `recall()` co-activation | sleep `edge_discovery` (cosine sim ≥ 0.5) | MCP `create_edge` tool |
| Weight | 0.0–3.0 co-activation amplitude | `cosine_sim` at creation (0.5–1.0) | user-supplied (default 1.0) |
| Decays? | yes — strengthens with co-recall, fades without | **no** — content similarity is a static property | **no** — user-asserted, never auto-removed |
| Maintenance | nightly Hebbian decay + prune below threshold | monthly `semantic_edge_reverify` drops edges with deleted endpoints | none — lives until the user deletes it |

> This is a **pedagogical analogy, not an implementation claim**. The system does not model the hippocampus or cortex directly — the parallel is in lifetime semantics: episodic-decay vs static-content vs user-pinned.

### Why three origins?

Conflating "recently co-recalled" with "content-similar" causes edge survival to scale as ~1/N² with context size — every doubled context loses a disproportionate share of its semantic edges to decay. The `origin` discriminator lets the decay loop touch only `hebbian` edges while `semantic` and `declared` survive as long as their endpoints exist.

### Edge origin vs memory scope

Edge `origin` and `Memory.scope` (`working` / `persistent`) are **two independent axes** managed by different subsystems:

- **`origin`** controls *edge* lifetime — see the table above.
- **`scope`** controls *memory node* lifetime — `working` memories are promoted to `persistent` (or archived) by the Sleep Consolidation phase based on access patterns and LLM judgment. See [Sleep Maintenance](sleep-maintenance.md).

Hebbian edges supply *signal* to consolidation (a working memory that gets reinforced via co-recall is more likely to be promoted), but Hebbian learning itself does not look at `scope` — `HebbianLearner.queue_update` creates edges between any co-activated pair regardless of whether the endpoints are `working` or `persistent`. In the neuroscience analogy, this is a two-level consolidation: edge consolidation (Hebbian → Semantic) and node consolidation (working → persistent) happen on different timescales and via different mechanisms.

For the migration history (PR #726 / Issue #722) and operator runbook, see [`docs/operations/semantic-edge-rollout.md`](operations/semantic-edge-rollout.md). For schema-level details, formulas, and the edge lifecycle flowchart, see [Neural Memory Engine § Edge Origins](architecture.md#edge-origins) in the architecture doc.

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

## Agent Memory Substrate

Kagura is a **knowledge store** for humans *and* an **agent memory substrate** for autonomous LLM loops. The substrate (epic #885, shipped v0.24.0 / v0.25.0) adds four primitives an agent loop needs that a pure knowledge base lacks — **delivery**, **trust**, **state**, and **feedback** — without inflating the memory taxonomy.

> **Design decision — primitives, not types.** A memory is classified by its `type` (`code`, `note`, `decision`, …) *and*, orthogonally, by **how it is delivered**. We deliberately did **not** add agent-loop memory types (Goal / Skill / Eval / Guardrail / …); the same effect is achieved with `delivery_mode` + dedicated lanes. See the rationale in [`docs/eval/retrieval-feedback-and-eval-gate.md`](eval/retrieval-feedback-and-eval-gate.md).

### 1. Delivery mode — *when* a memory surfaces

`delivery_mode` is orthogonal to `type`. It controls **when** a memory reaches the agent:

| `delivery_mode` | Surfaced | Read with | Use for |
|---|---|---|---|
| `on_recall` (default) | Probabilistically, via Hybrid Search | `recall()` | Ordinary knowledge |
| `always` | **Deterministically, every turn** | `load_pinned()` | An agent's Goal / Guardrail / critical policy |
| `on_trigger` | Inside a scheduled time window | `recall_upcoming()` | Deadlines, dated follow-ups (Time Memories, `type="time"`) |

- **`always` (pinned)** — `load_pinned()` returns the *complete, unranked* always-load set every call — the deterministic counterpart to probabilistic `recall()`. Pin with `remember(delivery_mode="always")` (or `update_memory(...)`); pinning also forces `scope="persistent"` so there is no sleep-consolidation wait. Unpin with `update_memory(delivery_mode="on_recall")`.
- **`on_trigger` (time)** — a Time Memory (`type="time"`, `details.trigger={year, month, day?}`) surfaces via `recall_upcoming()` when its window is upcoming. This is a deterministic time query, not semantic search.

### 2. Trust tier — provenance & behaviour-influencing reads

Not all memories are equally trustworthy as *instructions*. Connector-ingested content (Slack/Discord/etc.) is data, not commands — treating it as instructions is an indirect prompt-injection vector ([OWASP LLM01 / LLM03](https://owasp.org/www-project-top-10-for-large-language-model-applications/)).

- **`Memory.source_type`** — server-stamped provenance, never client-trusted: one of `manual`, `file`, `url`, `vault`, `api`, `connector` (NOT NULL + CHECK).
- **`Context.trust_tier`** — `trusted` (default) or `external`. Connector-fed contexts are marked `external`.
- **Trust filter** — pass `recall(filters={"trust_tier": "trusted"})` for any **behaviour-influencing read** (one whose results are fed back to the model as context/instructions). It excludes memories from `external`-tier contexts **and** any `source_type="connector"` memory (defence-in-depth). Default recall returns everything; the filter is opt-in and protective on connector-mixed workspaces, a no-op on manual-only ones.

The session-start bootstrap recalls use `trust_tier: "trusted"` for exactly this reason.

### 3. Agent state lane — scratchpad, never recalled

An agent loop needs durable, *exact-match* scratch state (current task, plan step, cursor) that must **never** pollute semantic search. This lives in a **dedicated `agent_states` table**, structurally excluded from `recall()`:

- `set_state(context_id, key, value)` — upsert a JSON value under a key (optional `ttl_seconds`, clamped to 30 days).
- `get_state(context_id, key)` — read one key, or omit `key` to list all live state for the context.
- Expired entries are reaped lazily on read. REST equivalents live under `/api/v1/contexts/{id}/state`.

### 4. Retrieval feedback signal — explicit, gated

`access_count` / `last_used` are weak implicit proxies for "was this recall useful". The **feedback signal** makes that judgment explicit and attributable:

- `feedback(context_id, memory_id, helpful, query?, note?)` — recording is **read-adjacent** (a `Viewer` who consumes recall may rate it).
- Stored **append-only** in a dedicated `retrieval_feedback` table — a time series (contradicting signals are kept), embedded nowhere, structurally excluded from `recall()`.

> **Eval-gate policy (HARD RULE).** Feedback is *collected* now; it is **not acted on automatically**. No self-update / auto-promotion loop ships before the golden retrieval eval gate (#344) is green — otherwise the substrate degrades into noisy implicit RL that optimizes for confident-but-wrong results. The golden eval harness lives in [`backend/tests/eval/`](../backend/tests/eval/README.md); the full policy is in [Retrieval Feedback & Eval Gate](eval/retrieval-feedback-and-eval-gate.md).

See [Architecture › Agent Memory Substrate](architecture.md#agent-memory-substrate-lanes) for the table-level design and how these lanes sit beside `memories`.

## MCP Tools

Kagura Memory Cloud exposes 50 tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), grouped into 12 categories:

| Category | Tools | Purpose |
|----------|-------|---------|
| Memory | 6 | `remember`, `recall`, `reference`, `update_memory`, `forget`, `explore` — store / search / discover memories |
| Agent Substrate | 5 | `load_pinned`, `recall_upcoming`, `set_state`, `get_state`, `feedback` — delivery-mode-aware retrieval, agent state lane, feedback signal (see [Agent Memory Substrate](#agent-memory-substrate)) |
| Neural Edges | 4 | `list_edges`, `create_edge`, `update_edge`, `delete_edge` — manage the Hebbian graph manually |
| Contexts | 7 | `get_context_info`, `list_contexts`, `create_context`, `update_context`, `delete_context`, `merge_contexts`, `update_search_config` |
| Tags | 1 | `list_tags` — tag vocabulary discovery for alignment before `remember`/`recall` |
| Files / R2 | 5 | `init_file_upload`, `complete_file_upload`, `list_files`, `get_file_download_url`, `delete_file` — binary attachments via R2 |
| Analyses (Memory Analysis) | 5 | `analyze_context`, `list_analyses`, `get_analysis`, `get_active_analysis`, `get_cluster` — large-scale qualitative clustering (Owner + Pro plan) |
| Resources | 6 | `setup_resource`, `setup_connector`, `list_resource_tokens`, `ingest_events`, `get_resource_impact`, `get_resource_schema` — external data ingestion + connector provisioning |
| Secrets | 5 | `secret_register_pubkey`, `secret_put`, `secret_get`, `secret_list`, `secret_revoke_grant` — zero-knowledge secret store (age ciphertext; server never decrypts) |
| Sleep Maintenance | 3 | `get_sleep_history`, `get_sleep_report`, `rollback_sleep_run` — background consolidation observability |
| Usage | 1 | `get_usage` — workspace quota and usage queries |
| API-Key Bindings | 2 | `list_my_bindings`, `describe_binding` — introspect public-bound API keys (read-only, owner-scoped) |

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
