# Core Concepts

This document explains the fundamental concepts of Kagura Memory Cloud.

> **Mental model**: Kagura is a team-scale **LLM Knowledge Base** following the 5-layer pattern from [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — Ingest, Compile, Index, Query, Enhance. The concepts below map onto these layers: a Workspace owns Contexts (Index unit), each Context holds Memories (Compile unit, 3-layer schema), and the Neural Memory graph + Sleep Maintenance drive Enhance. See [Architecture](architecture.md#llm-knowledge-base--5-layer-mapping) for the full mapping.

## Workspace

A **Workspace** is the top-level organizational unit — think of it as a team or organization.

- All resources (contexts, memories, members) belong to a workspace
- Plan limits (memory count, API quotas, context count) are enforced per workspace
- Plans are S / M / L / XL (internal names `free` / `basic` / `pro` / `promax`, `backend/src/config/plan_tiers.py`); limits such as contexts, members, memories and Sleep contexts come from the plan plus any add-ons
- One user owns 1 workspace, plus the slots granted by the highest plan among the workspaces they own (L: 3 in total, XL: 20) and any per-user bonus slots; joined workspaces (via invitation) are uncapped
- Each workspace has role-based access control (RBAC)

**Workspace Roles:**

| Role | Permissions |
|------|------------|
| **Owner** | Full access — billing, members, contexts, memories, settings |
| **Admin** | Manage members and shared contexts, read/write memories |
| **Member** | Read/write memories in assigned contexts |
| **Viewer** | Read-only access to assigned contexts |

**Context access list** (`allowed_context_ids`, set per member and viewer on the members page or in the invitation; through the API it can be given when the member is added, `POST /api/v1/workspaces/{id}/members`, and changed with `PUT …/members/{user_id}/context-access`, both open to a workspace-owner API key — see [Workspace Member APIs](api-reference.md#workspace-member-apis)):

| Role | Not set (`null`) | Empty (`[]`) | A list |
|------|------------------|--------------|--------|
| **Owner / Admin** | Every context (the list is ignored) | Every context | Every context |
| **Member** | No context until one is assigned | No context | Exactly these contexts |
| **Viewer** | Every shared context | No context | Exactly these contexts |

On top of the list, a private context stays open only to its creator, and a viewer only reads. Every context path applies the list — memory reads and writes, memory ids, context settings, files and listings — and a context outside it answers like one that does not exist. The list is kept when the member's role changes; a member without a list who becomes a viewer gets `[]`. A file uploaded without a context belongs to the workspace and stays visible to every member. The views that name no context follow the list too (#2325): `GET /api/v1/memory/list`, `/memory/stats` and `/memory/access-patterns` without `context_id` cover the caller's own memories in the contexts it reaches (a workspace-scoped API key: in its workspace), and the workspace totals (`GET /api/v1/workspaces/{id}/stats`, `/memory-timeline` without `context_id`) add up every context for an owner or admin and, for a member, the contexts it can open — as `/contexts/stats` does. The workspace card's `context_count` next to `max_contexts` stays the workspace-wide figure.

The dashboard (`GET /api/v1/workspace/stats`) uses the authenticated request's workspace; a workspace-scoped API key cannot read the account's separately selected workspace. The caller must be a member of that workspace: a session, or an account-wide key, whose selected workspace the user does not belong to gets the uniform `403` (`AUTH-101`), as on `GET /api/v1/workspace/embedding-status` and the `/workspace/usage/*` readouts, and the MCP `get_usage` tool answers `permission_denied`; a workspace-scoped key whose owner does not hold its workspace does not authenticate (`401`) (#2928). It names contexts, their creators and their searchability/erasure health only when the caller can open them. A member without a plan seat sees no such details until the seat is restored. Workspace-wide quota totals remain visible; the "Not accessible" row combines contexts hidden by privacy, the member's context access list or loss of a seat.

The context access dialog on the members page offers every shared context plus the member's own private contexts (marked Private), so a member who created a private context can be given it back after a role change; another member's private context is never offered (#2327). Adding a member or viewer to a context their list does not include would grant nothing, so `POST /api/v1/contexts/{id}/members` answers `409` (`details.reason`: `context_not_in_access_list`) unless the request sends `extend_access_list: true`, in which case a workspace owner or admin adds the context to that list in the same step; the web UI asks the operator first. A context member row left without effect by the list (for example after the context was removed from it) is repaired the same way.

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

**Context roles** (`context_members.role`): `owner` (settings and deletion), `editor` (read/write memories) and `viewer` (read-only). "Context editor or above" below means `editor` or `owner`.

**Naming rules:** lowercase alphanumeric + hyphens/underscores only (`^[a-z0-9_-]+$`)

## Memory

A **Memory** is a single piece of knowledge stored in Kagura Memory Cloud. Each memory uses a **3-layer architecture** optimized for search and retrieval:

```
┌─────────────────────────────────────────────────┐
│ Layer 1: Summary (10-500 chars, aim for 50+)    │
│   → Embedded as vector for semantic search      │
│   → Write the conclusion, not the process       │
│   ✅ "JWT expiry caused 401. Fixed with         │
│      refresh token rotation."                   │
│   ❌ "Discussed auth errors in meeting."        │
├─────────────────────────────────────────────────┤
│ Layer 2: Context Summary (optional, ≤2000 chars)│
│   → Why this memory matters                     │
│   → How and when to use it                      │
├─────────────────────────────────────────────────┤
│ Layer 3: Content + Details (full data)          │
│   → Complete code, documentation, procedures    │
│   → Structured metadata as JSON                 │
└─────────────────────────────────────────────────┘
```

Only the summary is embedded by default. A context can opt into **content chunks** (`content_chunks_enabled`, off by default and plan-gated) so long content is searchable too. Over MCP, a summary longer than 500 characters is cut at a sentence end (`summary_truncated: true`) rather than refused.

**Key attributes:**

- **type** — free-form, up to 50 characters (for example `decision`, `pattern`, `fact`, `troubleshooting`, `note`). Only `time` has a special meaning (Time Memories, below)
- **importance** — `0.0-1.0` (critical=0.9+, useful=0.6-0.8, reference=0.3-0.5)
- **tags** — For categorization and filtering (e.g., `["python", "auth", "jwt"]`)
- **scope** — `working` (short-term, the default) or `persistent` (long-term). Set by the server, not by `remember` / `update_memory`: Sleep consolidation promotes memories, and pinning (`delivery_mode="always"`) makes one persistent; a memory the resource indexer writes for a document of a resource or sync feed is persistent from creation
- **version** — 1 on create, +1 for each edit that changes the summary, context summary, content, details, type, importance or tags ([#2043](https://github.com/kagura-ai/memory-cloud/issues/2043))

**Versions and the revision log.** Several agents can share a context, so an edit can be made conditional: `update_memory(expected_version=N)` (REST: `PATCH` with `expected_version`) applies only while the memory is still at version N, and otherwise fails with `version_conflict` and the current version — nothing is lost to a concurrent edit. Each versioned edit records the values it overwrote (the changed fields only) and who made it in the memory's revision log, next to `created` and `forgotten` events. Maintenance that is not an edit — `forget`, Sleep's importance re-evaluation and tag merges, promotion, re-embedding, access statistics — never moves the version. `list_revisions(memory_id)` (REST: `GET /api/v1/memory/{memory_id}/revisions`) lists a memory's events with the fields each edit changed, and `reference(memory_id, version=N)` returns an earlier version rebuilt from them; a revert is an ordinary conditional edit with the old values. The log is not embedded or searchable; it stays with a forgotten memory's tombstone (both reads answer not-found once it is forgotten) and goes when the memory is purged.

**Sessions: who wrote it, and undoing one run.** Every write also records the writing agent and session ([#2044](https://github.com/kagura-ai/memory-cloud/issues/2044)) — on the revision-log event and as the memory's last writer. The agent comes only from the verified credential (an agent-bound API key), never from the request; the session is the request's OTel baggage session (`gen_ai.conversation.id` / `session.id`), else the MCP session id, so an MCP client gets one per connection without any change; `remember`, `update_memory` and `forget` return it as `session_id`. A session is an attribute, not a partition: recall stays cross-session, and a session is only ever addressed together with its principal — the same user and the same agent credential that wrote it (a context owner may name any). `changes_since(context_id, since, session_id=...)` lists what one session wrote; `rollback_session(context_id, session_id)` lists (dry run, the default) and then undoes it — created memories are forgotten, edited ones get the values from before the session's first edit back, forgotten ones are restored — skipping, with a cause, every memory someone else changed since. Memories written before #2044 carry no session.

## Hybrid Search

When you search with `recall()`, Kagura uses **Hybrid Search** combining two approaches:

```
┌──────────────────────┐    ┌──────────────────────┐
│  Semantic Search     │    │  Full-Text Search    │
│  (60% weight)        │    │  (40% weight)        │
│                      │    │                      │
│  Embedding vector    │    │  BM25 sparse vector  │
│  Vector similarity   │    │  Keyword matching    │
│  "What does it mean?"│    │  "What words match?" │
└──────────┬───────────┘    └──────────┬───────────┘
           │                           │
           └─────────┬─────────────────┘
                     ▼
            ┌─────────────────────┐
            │  Weighted fusion     │
            │  (max-normalised     │
            │   scores, 0.6 / 0.4) │
            └──────────┬──────────┘
                       ▼
            ┌─────────────────────┐
            │  Optional            │
            │  AI Reranking        │
            └─────────────────────┘
```

The 60 / 40 weights are per-context defaults (`update_search_config`). By default (`weighted`) each lane's scores are normalised by its maximum and then summed with the weights. A context's `fusion` setting picks the fusion: `auto` (the default, and the fusion of both search purposes below) uses `weighted` when the context's weights differ from 60 / 40 and otherwise the deployment's `HYBRID_FUSION` (`weighted`, `rrf`, or `keyword_floor`: the weighted list with the keyword lane interleaved 1:1), see [deployment](deployment.md#hybrid-fusion--issue-2958); `rrf` (reciprocal rank fusion, the weights unused) and `weighted` pin the context to that fusion whatever the deployment uses. A recall over several contexts uses the first context's search settings, fusion included. Embeddings come from the configured provider (OpenAI by default, or a self-hosted model). Vectors live in Qdrant, or in an embedded LanceDB store for the single-process "lite" setup (`vector_backend=lance`). Reranking is off by default (`use_rerank`), uses Voyage, Cohere or a self-hosted reranker when enabled, and is plan-gated.

**Search purposes** (#2983): the Web UI's search settings offer a Purpose select above the weights, so an owner can pick by what a context holds. *Conversation and work history* is semantic 0.6 / keyword 0.4 with fusion `auto` (the defaults); *Documents and manuals* (documents, manuals, FAQs) is semantic 0.8 / keyword 0.2 with fusion `auto`, which resolves to `weighted` at those weights, so a stored `weighted` with them shows as this purpose too. *Custom* is not a choice: the select shows it when the current values match neither purpose, and the controls below it set them. Choosing a purpose fills the weights and the fusion as unsaved changes; Save writes them through the REST search-config route (`PUT /api/v1/contexts/{id}/search-config`, the MCP twin is `update_search_config`). The purpose itself is not stored. The create dialog offers the two purposes (default: conversation and work history, which sends nothing beyond the create) and applies *Documents and manuals* through the same REST route right after the context is created; if that update fails, the context keeps the defaults, the error says why, and the settings can be changed later. Against a server that does not report a fusion, the purposes match and set the weights only.

**Keyword scoring.** The full-text lane scores like Okapi BM25 with field weights. Each memory is indexed by its summary, its context summary, the first 2,000 characters of its content and the reading of its summary (weights 2, 2, 1 and 0.5), Japanese text split into words by Sudachi. Each Japanese word is indexed, and searched, in Sudachi's normalized form: inflections become the dictionary form (走った → 走る) and spelling variants of one word become one spelling (引越し / 引っ越し → 引っ越し, 届出 / 届け出, 附属 / 付属, サーバ / サーバー, 打合せ / 打ち合わせ), so a query written one way finds a memory written the other way. Kana spellings of words usually written in kanji are normalized too (ある → 有る). Latin-script words, also inside Japanese text, and readings are not normalized. Query-time synonym expansion works on the same normalized forms, so a variant never counts as a synonym of itself. Within each field a repeated term counts less and less (term saturation, `k1` 1.2), and a match in a field longer than that field's average in the context counts less than the same match in a shorter one (length normalisation, `b` 0.75), so a long memory no longer outranks a short one by repeating common words. The averages are kept per context and follow it as it grows; a query term's rarity (IDF) is counted across the whole vector collection. A document ingested through a resource sync is one field with its own average. A recall over several contexts ranks each memory by its own context's averages. After an upgrade, `python -m src.cli.rebuild_keyword_vectors` brings the memories written before it to the same scoring (see [deployment](deployment.md#keyword-scoring-bm25--issue-2984)); a memory copied by a context merge keeps the source context's weighting until the rebuild or its next write. In keyword-only mode the scores are BM25 scores, so their spread is narrower than before and the `confidence` level computed from it can differ.

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
- **adoption** — deliberate `reference()` calls (log-capped; a read paged over
  several calls counts once);
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
  updated fact with `remember(..., supersedes=<old_memory_id>)`, by accepting
  a `supersede_candidate` with `update_memory(accept_supersede_candidate=true)`,
  or explicitly with `create_edge(edge_type="supersedes")` — each takes only
  your own memories at both ends (in a private context, a linked account's
  too; #1803, #2313). Without an edge, a
  judge-verified `supersede_candidate` folds the older version under the newer
  one in recall (`folded`, #2123) — the same effect, reversible, and off
  under `include_superseded=true`.
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

### Choosing a lane: search_mode and per-workload guidance

`recall()` accepts `search_mode`: `hybrid` (default), `semantic` (vector only),
or `keyword` (BM25 only, no embedding call). The pre-registered eval program
(300-doc frozen corpus) found that fixed-weight hybrid fusion does **not** beat
its own semantic component overall — but the per-bucket picture is
design-consistent:

| Your query traffic looks like… | Best lane |
|---|---|
| Exact IDs, error codes, commit SHAs, quoted literals, code symbols | `keyword` |
| Hiragana-heavy Japanese (embedding models struggle here) | `keyword` (Sudachi tokenization) |
| Natural-language questions, cross-source "what did we decide" queries | `semantic` |
| Genuinely mixed (a symbol + a real question around it) | `hybrid` |

### Query-intent routing (experiment-gated, #1212)

Instead of making every caller pick a lane, a context can opt into a
**deterministic query router** (no LLM on the hot path — regex and
character-class counting, sub-millisecond): set `routing_mode` via
`update_search_config`:

- `off` (default) — router never runs.
- `log_only` — the classifier's lane decision is stamped into telemetry
  (`query_router_decision`, numeric features only — never query text) with
  **zero ranking change**. Enable this first to measure your real traffic mix.
- `active` — `recall()` calls that **omit** `search_mode` use the routed lane.
  An explicitly passed `search_mode` always wins, in every mode. When the
  embedding cannot be computed (a provider error, a self-hosted inference
  server that cannot be reached) or the vector store fails, a routed
  `semantic` lane answers from keyword search with `degraded: true` and
  `degraded_reason`, as a hybrid search does (#2960). An explicitly passed
  `search_mode="semantic"` returns the error instead.

The router does not become the default until it clears the stage-3 calibration
gate on the frozen eval corpus (beat semantic-only overall AND beat the
strongest single component on each routed bucket) — the same bar fixed-weight
hybrid failed. The gate is runnable: `make eval-router-gate` measures the
lane arms, applies the pre-declared contracts, and persists per-bucket arm
performance to the `router_calibrations` store (see
`docs/eval/router-calibration-gate.md`, #1220).

## Neural Memory

**Neural Memory** creates automatic relationships between memories using brain-inspired algorithms.

### Edge Origins

Neural Memory edges are tagged with one of three **origins**, each with different lifetime semantics. This mirrors the **complementary learning systems** view from cognitive neuroscience — short-lived episodic associations vs durable content-based associations vs user-pinned ones.

| | **Hebbian** (hippocampus-like) | **Semantic** (cortex-like) | **Declared** |
|---|---|---|---|
| Encodes | "fired together recently" — episodic co-activation | "about similar things" — content neighborhood | "I'm telling you these are related" — explicit user assertion |
| Created by | runtime `recall()` co-activation | sleep `edge_discovery` (an LLM judges pairs in the 0.6–0.9 similarity band), plus weak k-NN seed edges when a memory is created | MCP `create_edge` tool |
| Weight | 0.0–3.0 co-activation amplitude | set at creation (seed edges stay below 0.5) | user-supplied (default 1.0) |
| Decays? | yes — strengthens with co-recall (when recall-time learning is enabled, `RECALL_ASSOCIATION_LEARNING_ENABLED`), fades without | **no** — content similarity is a static property | **no** — user-asserted, never auto-removed |
| Maintenance | nightly Hebbian decay + prune below threshold | monthly `semantic_edge_reverify` drops edges with deleted endpoints | none — lives until the user deletes it |

> This is a **pedagogical analogy, not an implementation claim**. The system does not model the hippocampus or cortex directly — the parallel is in lifetime semantics: episodic-decay vs static-content vs user-pinned.

### Why three origins?

Conflating "recently co-recalled" with "content-similar" causes edge survival to scale as ~1/N² with context size — every doubled context loses a disproportionate share of its semantic edges to decay. The `origin` discriminator lets the decay loop touch only `hebbian` edges while `semantic` and `declared` survive as long as their endpoints exist.

### Edge origin vs memory scope

Edge `origin` and `Memory.scope` (`working` / `persistent`) are **two independent axes** managed by different subsystems:

- **`origin`** controls *edge* lifetime — see the table above.
- **`scope`** is a *memory node* label — `persistent` means used, or old enough by rule; `working` is everything else. `working` memories are promoted to `persistent` by the Sleep Consolidation phase (or, without Sleep, the legacy consolidation task): by adoption-gated rules, plus an LLM judge for borderline cases when a deployment switches it on (`sleep_consolidation_judge_enabled`, off by default). `scope` has no effect on ranking, quota or retention: **a memory is kept until its owner or writer deletes it** (#3021) — `forget`, a duplicate merge the deployment enabled, and the documented retention of deleted rows are the only ways a row goes away. See [Sleep Maintenance](sleep-maintenance.md).

Hebbian edges supply *signal* to consolidation (a working memory that gets reinforced via co-recall is more likely to be promoted), but Hebbian learning itself does not look at `scope` — `HebbianLearner.queue_update` creates edges between any co-activated pair regardless of whether the endpoints are `working` or `persistent`. In the neuroscience analogy, this is a two-level consolidation: edge consolidation (Hebbian → Semantic) and node consolidation (working → persistent) happen on different timescales and via different mechanisms.

For the migration history (PR #726 / Issue #722) and operator runbook, see [`docs/operations/semantic-edge-rollout.md`](operations/semantic-edge-rollout.md). For schema-level details, formulas, and the edge lifecycle flowchart, see [Neural Memory Engine § Edge Origins](architecture.md#edge-origins) in the architecture doc.

### Activation Spreading

The `explore()` tool uses graph traversal to discover related memories (opt-in over MCP: `?add_tools=explore` or `?profile=full` on the URL):

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

**Sleep Maintenance** is a nightly background cycle that pays down memory debt asynchronously. Write paths optimize for ingest speed; over time this leaves near-duplicates, stale importance values, and graph gaps. Sleep runs per context; in `full` mode its phases are (`backend/src/services/sleep/orchestrator.py`):

1. **Edge Discovery** — find missing edges between related memories via medium-similarity search + LLM judgment (off by default: `sleep_edge_discovery_enabled`)
2. **Related index** — when enabled by its flag, record the related-memories index from discovery's evidence
3. **Dedup / Merge** — cluster high-similarity memories and merge duplicates
4. **Retention** — `merge_retention`, `forget_retention` and `measurement_retention` purge merge losers, forgotten memories and old measurements after their retention windows (no LLM)
5. **Importance Re-eval** — adjust importance via LLM scoring with EMA smoothing (off by default: `sleep_importance_reeval_enabled`)
6. **Consolidation** — promote working memories by rule (and by an LLM judge when switched on); nothing is archived
7. **Reindex** — re-embed memories changed by earlier phases so the vector store stays in sync with PostgreSQL

Each run is summarised in a sleep report.

**Resource-indexed memories are left to their source.** The memories the resource indexer writes (one per document version of a resource or sync feed) are persistent from creation, and Sleep never merges or re-scores them: dedup, importance re-evaluation and consolidation leave them out, and so does the consolidation task a deployment without Sleep runs. The source owns their lifecycle (a new version replaces the old one, a deletion removes it), whatever the context's settings, also after a sync target leaves feed management. What a person does to them (`forget`, `update_memory`, account erasure, deleting the context or workspace) is unchanged.

**A slug's documents follow the context that holds it.** One live context at a time holds a resource's slug. A context that is unpublished or changes its `resource_id` releases the slug and keeps the memories the indexer wrote for it. The search point of a document version belongs to the memory of the context that holds the slug: when another context takes the slug and indexes the same version, each memory the former holder kept becomes an ordinary memory with a search vector of its own, keeping its content, workspace and context, and a recall in its context still finds it. The same happens when such a memory is re-embedded before that (an edit, a Sleep reindex or restore, an embedding-model migration). From then on it is treated like any other memory (Sleep may merge or re-score it, an edit re-embeds its summary). Document updates still queued for the former holder are not applied; its deletes are, and a delete never removes a search point that a memory of another context uses.

Each context has a `sleep_mode` setting (`full`, `edges_only`, or `skip`; the default is `skip`) that controls which phases run. How many contexts may run Sleep depends on the plan (none on S and M, 3 on L, 15 on XL); a workspace whose plan suspends Sleep still runs the retention phases. Every action is recorded in `sleep_actions` and can be reversed via `rollback_sleep_run`.

See [Sleep Maintenance](sleep-maintenance.md) for the complete reference.

## Agent Memory Substrate

Kagura is a **knowledge store** for humans *and* an **agent memory substrate** for autonomous LLM loops. The substrate (epic #885, shipped v0.24.0 / v0.25.0) adds four primitives an agent loop needs that a pure knowledge base lacks — **delivery**, **trust**, **state**, and **feedback** — without inflating the memory taxonomy.

> **Design decision — primitives, not types.** A memory is classified by its `type` (`code`, `note`, `decision`, …) *and*, orthogonally, by **how it is delivered**. We deliberately did **not** add agent-loop memory types (Goal / Skill / Eval / Guardrail / …); the same effect is achieved with `delivery_mode` + dedicated lanes. See the rationale in [`docs/eval/retrieval-feedback-and-eval-gate.md`](eval/retrieval-feedback-and-eval-gate.md).

### 1. Delivery mode — *when* a memory surfaces

`delivery_mode` is orthogonal to `type`. It controls **when** a memory reaches the agent:

| `delivery_mode` | Surfaced | Read with | Use for |
|---|---|---|---|
| `on_recall` (default) | Probabilistically, via Hybrid Search | `recall()` | Ordinary knowledge |
| `always` | **Deterministically, every turn** | `load_pinned()` | Notes that stay relevant every turn, such as an agent's goal or a standing decision |
| `on_trigger` | Reserved. Time Memories are keyed on `type="time"`, not on this value — the write path never sets it | `recall_upcoming()` (by type) | Deadlines, dated follow-ups (Time Memories, `type="time"`) |
| *(any)* + `details.tool_trigger` | **At the matching tool call**, by a client-side hook | `load_guardrails()` (hooks cache it) | Tool guardrails — the lesson about one specific tool call |
| *(any)* + `details.tool_trigger`, client **without** hooks | **At connect** (server `instructions`, `?guardrails=<context_id>` or an agent default binding), **at session start** (`bootstrap.guardrails` / `get_context_info.guardrails`, default on) or **from an always-loaded file** (`AGENTS.md` export block) | a digest of `load_guardrails`' tool-triggered lane — summaries only ([Server instructions](mcp-tools.md#server-instructions)) | The same tool guardrails for ChatGPT web, ChatGPT Work web, Claude Desktop / Claude Chat and Codex cloud |

- **`always` (pinned)** — `load_pinned()` returns the *unranked* always-load set every call, up to its `cap` (1–1000; `truncated: true` when more exist) — the deterministic counterpart to probabilistic `recall()`. Pin with `remember(delivery_mode="always")` (or `update_memory(...)`); pinning also forces `scope="persistent"` so there is no sleep-consolidation wait. Unpin with `update_memory(delivery_mode="on_recall")`.
- **Time Memories** — a Time Memory (`type="time"`, `details.trigger={year, month, day?}`) surfaces via `recall_upcoming()` when its window is upcoming. This is a deterministic time query keyed on the type, not semantic search; `delivery_mode="on_trigger"` is accepted by the schema but nothing reads it.
- **Tool guardrails** — orthogonal to `type` and `delivery_mode`: any memory can carry `details.tool_trigger = {tool, on?, match?, action?}` (server-validated: a safe regex subset, patterns compiled once and never run on the server). `load_guardrails(context_id)` returns the trusted-tier pinned set plus every marked memory, each lane deterministic and capped on its own; client hooks cache that result and inject the matching summary at the tool call. Authoring needs context editor or above. Contract and cache format: [MCP Tools › Tool guardrails](mcp-tools.md#tool-guardrails).
- **WHERE axis (location)** — orthogonal to both `type` and `delivery_mode`: any memory can carry `details.location = {lat, lon, label?, text?}` (server-validated; `lat`/`lon` must be JSON numbers, not strings). `recall_nearby(context_id, lat, lon, radius_m=1000)` lists memories near a point, nearest first with `distance_m` — the deterministic spatial twin of `recall_upcoming()`. Coordinates belong ONLY in `details.location` (never in `context`, which is replicated into the search-index payload store), and `details` is replaced wholesale on update — resend `location` when updating details or it is dropped, or send `details_patch`, which merges into the stored details (a key set to `null` is deleted). Tags work the same way: `tags` replaces the list, `add_tags` / `remove_tags` edit it.
- **WHEN axis (event time)** — `created_at` / `updated_at` are write times; `details.observed_at` is when the fact held or the event happened ([#2330](https://github.com/kagura-ai/memory-cloud/issues/2330)). Any memory can carry it, as an ISO 8601 extended-form date (`2024-03-07`) or datetime with seconds; a datetime is stored in UTC (`2024-03-07T01:00:00Z`; a naive one is read as UTC), and anything else is refused. Write it with `remember(observed_at=...)` / `update_memory(memory_id, observed_at=...)` (alone, that edits only the event time) or inside `details`. A date means the start of that day (UTC). `recall` and `list` filter it with `observed_after` (inclusive) / `observed_before` (exclusive), where a memory without one never matches; `list(order_by="observed_at")` sorts by it with those memories last; rows carry `observed_at` when it is set, and the recall selector dates each row by it ("observed") before the write date. Set it to the date of the material, never the write time. It lives in `details`, so revisions and rollbacks keep it, and account erasure removes it with the rest of `details`.

### 2. Trust tier — provenance & behaviour-influencing reads

Not all memories are equally trustworthy as *instructions*. Connector-ingested content (Slack/Discord/etc.) is data, not commands — treating it as instructions is an indirect prompt-injection vector ([OWASP LLM01 / LLM03](https://owasp.org/www-project-top-10-for-large-language-model-applications/)).

- **`Memory.source_type`** — provenance: one of `manual`, `file`, `url`, `vault`, `api`, `connector` (NOT NULL + CHECK). A client may set the user-origin values (`file` / `url` / `vault` / `api` / `manual`) on import; `connector` is stamped by the server only. Trust is decided by the context's tier, not by this field.
- **`Context.trust_tier`** — `trusted` (default) or `external`. Connector-fed contexts are marked `external`.
- **Trust filter** — pass `recall(filters={"trust_tier": "trusted"})` for any **behaviour-influencing read** (one whose results are fed back to the model as context/instructions). It excludes memories from `external`-tier contexts **and** any `source_type="connector"` memory (defence-in-depth). Default recall returns everything; the filter is opt-in and protective on connector-mixed workspaces, a no-op on manual-only ones.

`bootstrap` reads only deterministic lanes (pinned, upcoming, changes — no recall), and `get_agent_bootstrap`'s optional recall is trusted-only, for exactly this reason; a follow-up topic recall at session start should pass `trust_tier: "trusted"` too. `load_guardrails` applies the same gate unconditionally — there is no parameter to turn it off.

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

## Agent Memory & Context Control Plane (preview)

The control plane is a separate layer over the Agent Memory Substrate. The substrate defines *what an agent can load and retain*; the control plane identifies an agent workload, narrows its existing workspace permissions, and assembles a safe session-start bundle.

- **Agent Registry** — `agents` rows are workspace-scoped resources, not authentication principals. `active | suspended | retired` is a fail-closed lifecycle switch; `shadow | enforce` controls whether binding violations are observed or denied.
- **Agent-bound member keys** — an owner-provisioned member key may carry `api_keys.agent_id`. It still authenticates as the member and keeps the same RBAC ceiling.
- **Subtractive context bindings** — `agent_context_bindings` computes effective access as existing RBAC ∩ binding. A binding can never grant access. In `enforce` mode, an unbound context is denied with the same not-found shape used for inaccessible contexts.
- **Composed bootstrap** — `get_agent_bootstrap` combines the context info, pinned memories, an optional trusted-only recall, upcoming time memories, and agent state. Components fail softly; identity and authorization fail closed.

The v0.49.0 milestone shipped the registry, context-level bindings, agent-bound keys, bootstrap, W3C Trace Context/baggage correlation, and the append-only `memory_access_events` foundation. Correlation is observability, never authorization; credential-bound identity outranks explicit/bootstrap and baggage claims. Per-memory type/source filters (`allowed_memory_types` / `allowed_source_types`; `null` = all, `[]` = deny-all) are enforced on the memory-read lanes for enforce-mode agents since [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299). `memory_access_events` records recall, reference, remember, update, forget, load_pinned, bootstrap, feedback, explore, load_guardrails, list, changes_since and the time / location recall lanes, including binding denies (`backend/src/models/memory_access_event.py`). See the [F1 binding design](design/agent-registry-and-bindings.md), [F2 bootstrap contract](design/agent-bootstrap-contract.md), and [F4 correlation design](design/agent-otel-correlation.md).

## MCP Tools

Kagura Memory Cloud exposes 73 tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), grouped into 14 categories.

**Tool profiles.** `tools/list` answers with the 21-tool `core` profile unless the MCP URL asks for more: `?profile=full` lists all 73, `?add_tools=<name,...>` adds tools to a profile, `?tools=<name,...>` picks an exact set, and `?profile=google` / `?profile=microsoft` serve the core set shaped for Google connected apps and Microsoft 365 Copilot (#2773, #2778). A profile only changes what is listed — every tool stays callable. The core set is `remember`, `remember_batch`, `update_memory`, `recall`, `reference`, `recall_upcoming`, `load_pinned`, `forget`, `create_edge`, `get_context_info`, `list_contexts`, `list_tags`, `create_context`, `update_context`, `delete_context`, `feedback`, `guide`, `describe_tools`, `list`, `changes_since` and `bootstrap` (`backend/src/mcp_server/tools/_profiles.py`). `describe_tools` and `guide` are how a client finds and learns the tools it does not list.

| Category | Tools | Purpose |
|----------|-------|---------|
| Discovery | 2 | `guide`, `describe_tools` — the per-tool manual and the schemas of tools outside the listed profile |
| Memory | 9 | `remember`, `remember_batch`, `recall`, `recall_nearby`, `reference`, `list_revisions`, `update_memory`, `forget`, `explore` — store / search / discover memories, read a memory's revision history |
| Agent Substrate | 12 | `load_pinned`, `load_guardrails`, `recall_upcoming`, `set_state`, `get_state`, `record_measurement`, `recall_series`, `feedback`, `list`, `changes_since`, `bootstrap`, `rollback_session` — delivery-mode-aware retrieval, deterministic listing and change log, one-call session start, undoing one session's writes, agent state lane, measurement series, feedback signal (see [Agent Memory Substrate](#agent-memory-substrate)) |
| Agent Control Plane (preview) | 10 | `register_agent`, `list_agents`, `get_agent`, `update_agent`, `delete_agent`, `bind_agent_context`, `list_agent_bindings`, `update_agent_binding`, `unbind_agent_context`, `get_agent_bootstrap` — registry, subtractive scoping, and session bootstrap |
| Neural Edges | 4 | `list_edges`, `create_edge`, `update_edge`, `delete_edge` — read the graph and manage declared edges (`create_edge` writes `origin='declared'`; edge types: `neural_association`, `related_to`, `depends_on`, `learned_from`, `continues_from`, `references_file`, `supersedes`, `contradicts`) |
| Contexts | 8 | `get_context_info`, `list_contexts`, `list_workspaces`, `create_context`, `update_context`, `delete_context`, `merge_contexts`, `update_search_config` — `list_workspaces` names the account's other workspaces, `list_contexts(workspace_id=...)` lists one |
| Tags | 1 | `list_tags` — tag vocabulary discovery: a tag review, or the stored spellings for a `recall` tag filter (a write aligns its spellings with `tags_normalize=true`, no `list_tags` call first) |
| Files / R2 | 5 | `init_file_upload`, `complete_file_upload`, `list_files`, `get_file_download_url`, `delete_file` — binary attachments via R2 |
| Analyses (Memory Analysis) | 5 | `analyze_context`, `list_analyses`, `get_analysis`, `get_active_analysis`, `get_cluster` — large-scale qualitative clustering (Owner + Pro plan; per-run size cap `ANALYSIS_MAX_MEMORY_COUNT`, default 10,000 memories — preview and start reject over-cap contexts with a 422 naming the limit) |
| Resources | 6 | `setup_resource`, `setup_connector`, `list_resource_tokens`, `ingest_events`, `get_resource_impact`, `get_resource_schema` — external data ingestion + connector provisioning |
| Secrets | 5 | `secret_register_pubkey`, `secret_put`, `secret_get`, `secret_list`, `secret_revoke_grant` — zero-knowledge secret store (age ciphertext; server never decrypts) |
| Sleep Maintenance | 3 | `get_sleep_history`, `get_sleep_report`, `rollback_sleep_run` — background consolidation observability |
| Usage | 1 | `get_usage` — workspace quota and usage queries |
| API-Key Bindings | 2 | `list_my_bindings`, `describe_binding` — introspect public-bound API keys (read-only, owner-scoped) |

See the [MCP Tools Reference](mcp-tools.md) for the full per-tool table with descriptions and required roles.

**Typical workflow:**

```
list_contexts()           → Discover available contexts (skip when the URL sets ?context=)
  ↓
bootstrap(context_id)     → Open the session: context notes, guardrails, pinned,
                            upcoming and recent changes in one call
  ↓
recall(query)             → Search for relevant memories
  ↓
reference(memory_id)      → Get full details
  ↓
remember(summary, content) → Store new knowledge
  ↓
explore(memory_id)        → Find related memories (opt-in: ?add_tools=explore)
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
