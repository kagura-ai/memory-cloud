# MCP Tool Input Schemas — pre-1.0 surface enumeration

> Issue: #622 — pre-1.0 public API surface enumeration and freeze
> Enumerated at commit 20ae959a2c79cacd2cf7922512ad780f540e9c60 (main HEAD, 2026-06-12)
> Source: backend/src/mcp_server/tools/_definitions.py — 45 tools, 45 of 45 enumerated
> Re-frozen after #990 (Phase 1): additionalProperties:false on all 45 schemas; analyze_context `query` no-op removed.
> Re-frozen after #990 (Phase 2): merge_contexts `source_id`/`target_id` → `source_context_id`/`target_context_id` (handler keeps the old names for one release as a deprecated alias; kagura-memory-python-sdk#196 tracks the SDK); analyze_context's internal `model_id` (llm_pricing.id int PK) **removed** from the public MCP surface (run uses server-default model); `k`/`limit`/`cap` result-size trio **consciously frozen** as three distinct conventions (see below). The cross-surface stable-model-identifier replacement (REST + MCP) stays deferred to the v1.5 per-workspace model selection. #990 is now resolved on the MCP surface.

**Global note**: ✅ **Resolved in #990.** Every tool `inputSchema` now sets `additionalProperties: false` — undeclared top-level parameters are forbidden by the published contract. Applied centrally in `get_tool_definitions()` so all 45 tools stay uniform and any new tool inherits the policy automatically. The policy is advisory at the server (handlers read args defensively via `.get` and never Pydantic-validate), so it tightens the client-facing contract without changing server behaviour; nested object params are unaffected (only the top-level argument object is closed).

---

### list_my_bindings

List the public-bound API keys you own (read-only introspection; Issue #629).

- **Required**: none
- **Optional**: none (empty `properties`)

### describe_binding

Describe one of your public-bound API keys by exactly one selector (read-only).

- **Required**: none ⚠ schema requires nothing, but the contract is "EXACTLY ONE of key_id / context_id" — not expressible as written; a `oneOf` (or at least one required selector) would harden the 1.0 surface
- **Optional**:
  - `key_id` — integer (from list_my_bindings; mutually exclusive with context_id)
  - `context_id` — string, format `uuid` (bound public context; mutually exclusive with key_id) ⚠ here `context_id` means "selector for a binding lookup", whereas in nearly every other tool it means "the context to operate in" — same name, different role

### remember

Store information into long-term memory with 3-layer architecture (summary / context_summary / details).

- **Required**:
  - `summary` — string (10–500 chars, per description; no `minLength`/`maxLength` constraint in schema ⚠ documented limits not schema-enforced)
  - `content` — string
  - `type` — string (free-form; examples: 'code', 'note', 'decision', 'bug-fix', 'feature', 'config', 'learning') ⚠ free string with magic value `'time'` (Time Memory, see recall_upcoming) not listed among the examples — enum-vs-open-vocabulary decision should be explicit before freeze
  - `context_id` — string, format `uuid`
- **Optional**:
  - `context_summary` — string (max 2000 chars, description-only)
  - `details` — object
  - `importance` — number (0.0–1.0, default 0.5 per description; no schema `default`)
  - `tags` — array of string
  - `context` — object ⚠ `context` (metadata object) vs `context_id` (UUID) vs `context_summary` (string) in the same tool is a confusing namespace; `metadata` would be clearer
  - `delivery_mode` — string, enum [`always`, `on_recall`, `on_trigger`] (default `on_recall` per description)
  - `source_uri` — string (max 2048 chars, description-only)
  - `source_type` — string, enum [`file`, `url`, `vault`, `api`, `manual`] ⚠ enum lock-in risk: `vault` encodes an Obsidian-specific integration; new origins (e.g. slack, notion, email) will force enum growth on a frozen surface
  - `linked_memory_ids` — array of string (format `uuid`)
  - `linked_source_uris` — array of string

### update_memory

Update a memory in-place (by memory_id) or upsert by external_id.

- **Required**: `context_id` — string, format `uuid`
- **Optional**:
  - `memory_id` — string, format `uuid` (in-place mode)
  - `external_id` — string (upsert lookup key) ⚠ external-identity naming: `external_id` here vs `doc_id` in ingest_events vs `source_uri` in remember — three names for "stable external identifier" across the surface
  - `summary` — string (required for upsert mode — mode-conditional requirement not expressible in current schema ⚠ "provide exactly one of memory_id / external_id" plus mode-dependent required fields are description-only contracts; consider `oneOf` before freeze)
  - `content` — string (required for upsert mode)
  - `type` — string (required for upsert mode)
  - `context_summary` — string (max 2000 chars)
  - `details` — object
  - `importance` — number (0.0–1.0)
  - `tags` — array of string
  - `context` — object
  - `delivery_mode` — string, enum [`always`, `on_recall`, `on_trigger`]

### recall

Hybrid search (semantic + BM25 + Neural Memory boosting) over memories; returns Layers 1–2.

- **Required**: `query` — string. ✅ **CORRECTED (v0.34.0 review): `context_id` is NOT unconditionally required.** The handler requires *either* `context_id` *or* `context_ids` (`if "context_id" not in args and "context_ids" not in args: error`), so cross-context recall via `context_ids` alone is a valid call. #1054 had added `context_id` to the schema's `required` array on the false premise that "the handler always required it" — that rejected legitimate `context_ids`-only calls at any schema-validating client/gateway, and has been reverted (`required: ["query"]`). The `context_id`/`context_ids` pair is a description-only "exactly one of" contract — left out of `required`, enforced at the handler — the same convention as `forget(memory_id/query)` and `describe_binding(key_id/context_id)`.
- **Optional**:
  - `k` — integer (default 5, max 100 per description) ✅ **DECIDED (#990): the `k`/`limit`/`cap` trio is consciously frozen as three distinct conventions, not unified.** `k` = the top-k count for a *relevance-ranked* result set (recall/get_sleep_history — established ML term); `limit` = pagination size for a *flat list* (the list_* family); `cap` = the ceiling for *pinned-memory load* (load_pinned). Unifying to one name spans ~8 tools (breaking) and would erase the relevance-vs-pagination signal `k` carries — net DX loss. Frozen as-is.
  - `use_rerank` — boolean (default false)
  - `filters` — object (free-form: type, tags, tags_match, importance gte, created/updated bounds, source_uri_prefix, source_type, trust_tier) ⚠ filter vocabulary lives only in prose; analyze_context exposes the equivalent filters (`types`, `tags`, `min_importance`, `from`/`to`) as top-level params instead — two filter styles on one surface
  - `context_ids` — array of string (format `uuid`), minItems 2, maxItems 20 (cross-context recall; an ALTERNATIVE to `context_id` — `context_ids` overrides `context_id` when both are given, and is sufficient on its own). The deliberate frozen pattern: singular `context_id` and plural `context_ids` are the two arms of one "exactly one of" selector, neither in `required`.
  - `search_mode` — string, enum [`hybrid`, `semantic`, `keyword`] (default hybrid) ⚠ mild lock-in: description bakes in the 60/40 weighting and BM25/Neural-Memory implementation details; enum values themselves look stable
  - `include_explore_hints` — boolean (default false)
- ✅ `context_id` is not in `required` here (because `context_ids` can stand in) while it IS required on the remember-family (which has no plural form). This asymmetry is intentional and frozen: recall is the only tool with a cross-context (`context_ids`) mode. The prose still says "IMPORTANT: always specify context_id" as the single-context happy-path guidance for agents.

### reference

Retrieve full Layer-3 details of one memory by ID.

- **Required**:
  - `memory_id` — string, format `uuid`
  - `context_id` — string, format `uuid`
- **Optional**: none

### recall_upcoming

Deterministic time-window query over Time Memories (type='time'), soonest first.

- **Required**: `context_id` — string, format `uuid`
- **Optional**:
  - `from` — string (naive ISO lower bound) ⚠ range params are `from`/`until` here but `from`/`to` in analyze_context — upper-bound name differs across the surface
  - `until` — string (naive ISO upper bound)
  - `k` — integer (default 20, max 100) ⚠ same `k` name as recall but a different default (20 vs 5)

### load_pinned

Deterministically load the complete set of delivery_mode='always' memories for a context (counterpart to probabilistic recall).

- **Required**: `context_id` — string, format `uuid`
- **Optional**: `cap` — integer (1–1000; server default if omitted) ✅ **frozen (#990)** as the pinned-memory-load ceiling — distinct from `k` (relevance top-k) and `limit` (list pagination); see the recall `k` note for the rationale.

### forget

Soft-delete memories by specific memory_id or by search query (top-k matches); 30-day retention.

- **Required**: `context_id` — string, format `uuid` ⚠ neither `memory_id` nor `query` is required — schema permits a call with only context_id, which has no defined meaning; a `oneOf`/`anyOf` over the two modes should land before freeze (a destructive tool with an ambiguous-input hole is the worst place for it)
- **Optional**:
  - `memory_id` — string, format `uuid`
  - `query` — string (bulk-delete mode) ⚠ `query` means "search to delete" here, "search to recall" in recall/feedback, and "reserved, ignored" in analyze_context
  - `k` — integer (default 10; safety limit in query mode)

### explore

Graph traversal from a seed memory via activation spreading (Neural Memory).

- **Required**:
  - `memory_id` — string, format `uuid` (seed)
  - `context_id` — string, format `uuid`
- **Optional**:
  - `depth` — integer (default 2, max 5)
  - `min_weight` — number (schema `default: 0.05`, range 0.0–1.0)
  - `relation_types` — array of string ('neural_association', 'related_to', 'depends_on', 'learned_from', 'continues_from', 'references_file') ⚠ named `relation_types` here but `edge_types` in list_edges and `edge_type` in create_edge/update_edge — same vocabulary, two names; also a string array here vs a hard enum in create_edge/update_edge

### list_edges

List outgoing/incoming Neural Memory graph edges for a memory.

- **Required**:
  - `memory_id` — string, format `uuid`
  - `context_id` — string, format `uuid`
- **Optional**:
  - `min_weight` — number (schema `default: 0.0`) ⚠ default differs from explore's 0.05 for the same-named param (defensible — list vs traverse — but worth a deliberate call)
  - `edge_types` — array of string (same vocabulary as explore's `relation_types` ⚠ see explore)
  - `limit` — integer (max edges per direction)

### create_edge

Create a manual edge between two memories.

- **Required**:
  - `source_id` — string, format `uuid` (memory)
  - `target_id` — string, format `uuid` (memory)
  - `context_id` — string, format `uuid`
- **Optional**:
  - `edge_type` — string, enum [`neural_association`, `related_to`, `depends_on`, `learned_from`, `continues_from`, `references_file`], schema `default: "related_to"` ⚠ enum lock-in risk: `neural_association` exposes the internal Hebbian-learning mechanism as a public value (description even says "prefer 'related_to' for manual edges"); `continues_from`/`references_file` are producer-asserted types added in #782 — the set has already grown once and will again; consider an open string with server-side validation, or a documented extension policy
  - `weight` — number (0.0–3.0, schema `default: 1.0`)
  - `confidence` — number (0.0–1.0, schema `default: 1.0`)

### update_edge

Update an existing edge's weight and/or type (identified by source_id + target_id).

- **Required**:
  - `source_id` — string, format `uuid`
  - `target_id` — string, format `uuid`
  - `context_id` — string, format `uuid`
- **Optional**:
  - `weight` — number (0.0–3.0; deliberately no default — omitted = unchanged)
  - `edge_type` — string, same 6-value enum as create_edge (⚠ same lock-in risk)

### delete_edge

Hard-delete an edge between two memories.

- **Required**:
  - `source_id` — string, format `uuid`
  - `target_id` — string, format `uuid`
  - `context_id` — string, format `uuid`
- **Optional**: none
- ⚠ hard delete, while forget/delete_context/delete_file are soft deletes — the asymmetry is documented but worth confirming as intentional for 1.0

### get_context_info

Get a context's metadata, usage guidelines, and memory-tool instructions (session-start call).

- **Required**: `context_id` — string, format `uuid`
- **Optional**: `include_details` — boolean (default true per description)

### list_contexts

List available contexts in the workspace, most recently used first.

- **Required**: none
- **Optional**: `include_stats` — boolean (default false) ⚠ `include_stats` here vs `include_details` in get_context_info vs `include_revoked` in list_resource_tokens — the include_* family is fine, but stats/details naming for "memory-count breakdown" differs between these two sibling tools

### list_tags

List a context's tag vocabulary with usage counts and recency (tag-drift mitigation; Issue #614).

- **Required**: `context_id` — string, format `uuid`
- **Optional**:
  - `limit` — integer (1–500, default 50)
  - `min_count` — integer (default 1)
  - `sort` — string, enum [`count`, `recent`, `alpha`]
  - `prefix` — string (case-insensitive; %/_ escaped)

### create_context

Create a new context (namespace) in the workspace.

- **Required**: `name` — string (pattern `^[a-z0-9_-]+$` per description; ⚠ pattern is prose-only, no schema `pattern` constraint)
- **Optional**:
  - `display_name` — string (defaults to name)
  - `description` — string
  - `summary` — string (200–500 chars)
  - `usage_guide` — string
  - `is_private` — boolean (default true)
  - `embedding_model` — string ⚠ enum-adjacent lock-in: free string, but the description hardcodes provider/model names ('text-embedding-3-small', 'qwen3-embedding:8b') and dimension counts — model names will churn; keep free-form and move examples to docs

### update_context

Update an existing context's settings (role-gated per field).

- **Required**: `context_id` — string, format `uuid`
- **Optional**:
  - `display_name` — string (max 200 chars)
  - `description` — string (max 500 chars)
  - `summary` — string (max 500 chars)
  - `usage_guide` — string (max 2000 chars)
  - `resource_id` — string (lowercase alphanumeric + underscores) ⚠ note: hyphens not allowed here per description, but setup_resource/setup_connector allow hyphens for the same identifier — conflicting validation prose for one concept
  - `is_public` — boolean
  - `is_locked` — boolean

### delete_context

Soft-delete a context and all its memories (owner-only; locked contexts must be unlocked first).

- **Required**: `context_id` — string, format `uuid`
- **Optional**: none

### merge_contexts

Copy all memories from a source context into a target context (same embedding model, same workspace).

- **Required**:
  - `source_context_id` / `target_context_id` — string, format `uuid` (context) ✅ **renamed in #990** from `source_id`/`target_id` to remove the collision with the edge tools (which use those names for MEMORY UUIDs — this was the strongest cross-tool ambiguity on the surface). The handler still accepts the old `source_id`/`target_id` for one release as a deprecated alias (logs a warning); kagura-memory-python-sdk#196 tracks updating the SDK before the alias is removed.
- **Optional**: `delete_source` — boolean (default false)

### update_search_config

Tune per-context hybrid-search weights and reranker settings.

- **Required**: `context_id` — string, format `uuid`
- **Optional**:
  - `semantic_weight` — number (0.0–1.0, default 0.6)
  - `bm25_weight` — number (0.0–1.0, default 0.4; weights must sum to 1.0 — prose-only constraint) ⚠ `bm25_weight` exposes the algorithm name (BM25) in a frozen param name; `keyword_weight` would survive an algorithm swap
  - `fetch_factor` — integer (1–10, default 3) ⚠ leaks retrieval-pipeline implementation into the public surface
  - `use_rerank` — boolean
  - `reranker_provider` — string, enum [`voyage`, `cohere`, `self_hosted`] ✅ **DECIDED (#1054): now a hard enum** (was a free string; matches the DB CHECK constraint and aligns with connector_type's enum policy). Widening the enum later (adding a provider) is non-breaking; removing a value is a MAJOR bump. ⚠ **RENAMED (#1160): the `ollama` enum value → `self_hosted`.** Renaming (not widening) an enum value is a **breaking (MAJOR) change** under the frozen-1.0 policy; it ships as a **pre-1.0 exemption** to make the provider slot backend-neutral (self-hosted OpenAI-compatible: Ollama, vLLM). The distinct `ollama_cloud` provider is unaffected.
  - `reranker_model` — string (provider-specific model names in description)

### get_usage

Get current workspace usage and quota limits (plan, memories, contexts, members, mcp_calls_per_day).

- **Required**: none
- **Optional**: none (empty `properties`)

### get_sleep_history

List recent Sleep Maintenance runs for a context.

- **Required**: `context_id` — string, format `uuid`
- **Optional**: `limit` — integer (schema `default: 10`, max 50)

### get_sleep_report

Get one Sleep Maintenance report with its full action audit log.

- **Required**: `report_id` — string, format `uuid` ⚠ Sleep runs are addressed by `report_id` while analysis runs are addressed by `run_id` (get_analysis/get_cluster) — two names for "id of a background run" on one surface
- **Optional**: none

### rollback_sleep_run

Reverse all recorded actions of a completed Sleep Maintenance run (destructive; marks report rolled_back).

- **Required**: `report_id` — string, format `uuid` (⚠ tool is named *_sleep_run but takes report_id — run/report vocabulary is mixed even within the sleep bundle)
- **Optional**: none

### setup_resource

Atomically create a public context + resource token for an ingestion pipeline (Pro plan; owner/admin).

- **Required**:
  - `name` — string (lowercase alphanumeric/hyphen/underscore, max 100 chars — prose-only)
  - `resource_id` — string (lowercase alphanumeric/hyphen/underscore, max 255 chars, workspace-unique)
- **Optional**:
  - `display_name` — string
  - `description` — string (token description)
  - `quota_events_per_hour` — integer (1–10000, default 1000)

### setup_connector

Create an ai-worker chat-ingest connector (resource row + connector row + scoped token) in one operation.

- **Required**:
  - `connector_type` — string, enum [`slack`, `discord`, `teams`] ⚠ highest enum lock-in risk on the surface: a hard-frozen list of third-party chat vendors is guaranteed to grow (LINE, Mattermost, email…); every addition is a surface change — consider free string + server-side registry before freeze
  - `resource_id` — string (lowercase alphanumeric/hyphen/underscore, max 255 chars, workspace-unique)
- **Optional**:
  - `display_name` — string
  - `oauth_tokens` — object (stored Fernet-encrypted)
  - `pii_guardrail_config` — object
  - `litellm_virtual_key_id` — string ⚠ vendor lock-in in a param NAME: "LiteLLM" (an internal gateway choice) is baked into the frozen public surface; a neutral name (`gateway_key_id`/`virtual_key_id`) would survive a gateway swap
  - `virtual_key_valid_until` — string (ISO 8601) ⚠ uses `*_until` while analyze_context uses `to` — see from/until/to inconsistency
  - `quota_events_per_hour` — integer (1–10000, default 1000)

### ingest_events

Batch ingest resource events (upsert/delete), max 100 events per call, 100KB per payload.

- **Required**:
  - `resource_id` — string
  - `events` — array of object (max 100 — prose-only, no `maxItems` ⚠ documented limits not schema-enforced). Each event:
    - **Required**: `op` — string, enum [`upsert`, `delete`]; `doc_id` — string ⚠ `doc_id` vs update_memory's `external_id` — two names for "stable external document identity"
    - **Optional**: `version` — integer (required for upsert — mode-conditional, prose-only); `payload` — object (required for upsert); `idempotency_key` — string; `importance` — number (0.0–1.0, default 0.6 ⚠ differs from remember's importance default 0.5 — deliberate?); `event_metadata` — object ⚠ remember calls the same concept `context`; pick one metadata-bag name
- **Optional** (top level): none

### get_resource_impact

Get resource stats (active token count, memory count, current schema version) before schema changes.

- **Required**: `resource_id` — string
- **Optional**: none

### get_resource_schema

Get a resource's field schema definition (latest or a historical version).

- **Required**: `resource_id` — string
- **Optional**: `schema_version` — integer (default: latest)

### list_resource_tokens

List resource-token metadata for the workspace (owner/admin; no plaintext tokens).

- **Required**: none (explicit `"required": []` — only tool in the file to spell it out)
- **Optional**:
  - `resource_id` — string (filter)
  - `limit` — integer (1–100, default 50)
  - `offset` — integer (default 0) ⚠ only offset-paginated tool; list_analyses/get_cluster use opaque `cursor` — two pagination models frozen side by side
  - `include_revoked` — boolean (default true) ⚠ defaulting to INCLUDE revoked tokens is a surprising default for a list endpoint; most list tools exclude dead rows by default (list_files excludes deleted, list_contexts excludes deleted)

### analyze_context

Start (or dry-run cost-preview) a Memory Analysis run that clusters a context's memories into themes.

- **Required**: `context_id` — string ⚠ missing `format: "uuid"` — the analysis bundle (analyze_context, list_analyses, get_active_analysis), feedback, set_state, get_state all drop the uuid format annotation that the rest of the surface carries on context_id
- **Optional**:
  - `from` — string (ISO-8601 lower bound) / `to` — string (upper bound) ⚠ recall_upcoming uses `from`/`until` — unify before freeze
  - `types` — array of string ⚠ top-level filter params (`types`, `tags`, `min_importance`) duplicate recall's `filters` object vocabulary with different shapes (`types` plural array vs filters `type` singular; `min_importance` vs filters `importance: {gte}`)
  - `tags` — array of string
  - `min_importance` — number (0.0–1.0)
  - ~~`query`~~ — ✅ **removed in #990** (was a reserved-for-v1.5 no-op; will be re-added when the query-scoped path is implemented).
  - ~~`model_id` — integer~~ ✅ **removed from the public MCP surface in #990** — it exposed an internal `llm_pricing.id` DB PK and was unusable without knowing it; analyze_context now always uses the server-default model (`AnalysisParams.model_id` defaults to None → orchestrator's default-row path). **Note:** the *REST* `model_id` (int) on `api/routes/analyses.py` is unchanged (out of #990's MCP scope), and the **stable `(provider, model)` identifier replacement** is part of the v1.5 per-workspace model-selection redesign (`Workspace.analysis_default_model_id`) — a cross-surface change, not an MCP-schema-only edit.
  - `dry_run` — boolean (default false)

### get_analysis

Fetch one analysis run by id (workspace-scoped; uniform run_not_found).

- **Required**: `run_id` — string ⚠ no `format: "uuid"` despite being documented as a UUID; ⚠ `run_id` vs sleep tools' `report_id`
- **Optional**: none

### list_analyses

List analysis runs for a context, newest first, cursor-paginated.

- **Required**: `context_id` — string (⚠ no uuid format)
- **Optional**:
  - `limit` — integer (1–100, default 20)
  - `cursor` — string (opaque)

### get_active_analysis

Return the most recent succeeded analysis run for a context.

- **Required**: `context_id` — string (⚠ no uuid format)
- **Optional**: none

### get_cluster

Drill into one cluster of an analysis run (label, representatives, paginated members).

- **Required**:
  - `run_id` — string (⚠ no uuid format)
  - `cluster_index` — integer (zero-based, stable)
- **Optional**:
  - `limit` — integer (1–200, default 50) ⚠ max differs from list_analyses' 100 and list_files' 500 — per-tool caps are fine but should be tabulated in the freeze doc
  - `cursor` — string (opaque)

### init_file_upload

Reserve quota and return a presigned PUT URL for an R2 file upload (100 MiB Phase-1 cap; sha256 dedup).

- **Required**:
  - `filename` — string
  - `content_type` — string (MIME)
  - `size_bytes` — integer
  - `sha256` — string (lower-case hex; no `pattern` constraint ⚠ format prose-only)
- **Optional**: `workspace_id` — string ⚠ "Overrides the authenticated workspace_id" appears on all 5 file tools and nowhere else on the surface. Every other tool scopes implicitly by auth (+ context_id). An auth-override parameter is both an inconsistency and a security-review item — if it's an internal/admin affordance it should not be in the frozen public schema

### complete_file_upload

Finalize an upload after the client PUTs bytes (verifies R2 object + sha256/size; idempotent).

- **Required**:
  - `file_id` — string ⚠ no `format: "uuid"` despite "UUID returned by init_file_upload" (same gap on all four file_id params)
  - `sha256` — string
- **Optional**: `workspace_id` — string (⚠ see init_file_upload)

### get_file_download_url

Return a short-lived presigned GET URL for an uploaded file (read-only).

- **Required**: `file_id` — string (⚠ no uuid format)
- **Optional**: `workspace_id` — string (⚠ see init_file_upload)

### delete_file

Soft-delete a file; quota released immediately, binary swept after 7 days.

- **Required**: `file_id` — string (⚠ no uuid format)
- **Optional**: `workspace_id` — string (⚠ see init_file_upload)

### list_files

List uploaded, non-deleted files in the workspace, newest first (read-only).

- **Required**: none
- **Optional**:
  - `limit` — integer (1–500, default 50)
  - `workspace_id` — string (⚠ see init_file_upload)

### feedback

Record whether a recalled memory was helpful for a query (append-only; excluded from recall; Issue #888).

- **Required**:
  - `context_id` — string (⚠ no uuid format — newest tools #888/#889 dropped the annotation)
  - `memory_id` — string (⚠ no uuid format)
  - `helpful` — boolean
- **Optional**:
  - `query` — string (max 1024 chars)
  - `note` — string (max 2000 chars)

### set_state

Upsert TTL-bounded agent run-state at (context_id, key); excluded from recall (Issue #889).

- **Required**:
  - `context_id` — string (⚠ no uuid format)
  - `key` — string (max 255 chars)
  - `value` — untyped (any JSON value; only schema-less property on the surface — intentional, but worth an explicit note in the freeze doc)
- **Optional**: `ttl_seconds` — integer (clamped to 2592000 = 30 days)

### get_state

Read one key or list all live state entries for a context (read-only).

- **Required**: `context_id` — string (⚠ no uuid format)
- **Optional**: `key` — string (omit to list all live entries)

---

## Cross-tool naming observations

Recurring inconsistencies (each instance flagged inline above):

1. **Result-size parameter — three names** ✅ **RESOLVED (#990): consciously frozen as three distinct conventions, not unified** — `k` (relevance top-k: recall, recall_upcoming, forget), `limit` (list pagination: list_edges, list_tags, get_sleep_history, list_resource_tokens, list_analyses, get_cluster, list_files), `cap` (pin-load ceiling: load_pinned). The differing `k` defaults (5 vs 20 vs 10) are per-tool intent, not drift. See the recall `k` note for the rationale.
2. **`source_id`/`target_id` overload** ✅ **RESOLVED (#990): merge_contexts renamed to `source_context_id`/`target_context_id`** (old names kept one release as a deprecated handler alias; kagura-memory-python-sdk#196 tracks the SDK). The edge tools keep `source_id`/`target_id` for MEMORY UUIDs — no longer ambiguous.
3. **Time-range bounds**: `from`/`until` (recall_upcoming) vs `from`/`to` (analyze_context) vs `*_valid_until` (setup_connector).
4. **Edge-type vocabulary — three param names and two typings**: `relation_types` (array of free string, explore) vs `edge_types` (array of free string, list_edges) vs `edge_type` (hard 6-value enum, create_edge/update_edge).
5. **`format: "uuid"` coverage is inconsistent**: present on the core memory/context/edge tools, absent from the entire analysis bundle (`context_id`, `run_id`), all file tools (`file_id`), and the newest tools (feedback, set_state, get_state). The newer the tool, the less schema rigor — exactly the drift a freeze should stop.
6. **Two pagination models**: `offset` (list_resource_tokens) vs opaque `cursor` (list_analyses, get_cluster); everything else is single-page `limit`-capped.
7. **Background-run identity**: `report_id` (sleep bundle) vs `run_id` (analysis bundle); rollback_sleep_run is even named "run" while taking `report_id`.
8. **External document identity**: `external_id` (update_memory) vs `doc_id` (ingest_events) vs `source_uri` (remember) — three lanes, three names.
9. **Metadata bag**: `context` (remember/update_memory — clashing with `context_id` and `context_summary` in the same tool) vs `event_metadata` (ingest_events).
10. **Filter style**: recall packs filters into one free-form `filters` object (singular `type`, `importance: {gte}`); analyze_context spreads them top-level (`types` plural, `min_importance`).
11. **`workspace_id` override** exists only on the 5 file tools; the rest of the surface scopes by auth + context_id. Inconsistent and security-sensitive.
12. **Constraints live in prose, not schema**: char limits (summary 10–500, context_summary 2000), `maxItems` (events ≤ 100), patterns (context name, sha256 hex), numeric ranges and most defaults are description-only. (`additionalProperties` ✅ now set to `false` on all 45 schemas in #990 Phase 1 — the rest of the prose-only constraints remain a P2 hardening item.) Tool-side validation still cannot fully rely on the published schema.
13. **Enum policy** ✅ **RESOLVED (#1054)**: reranker_provider is now a hard enum [`voyage`,`cohere`,`self_hosted`], matching connector_type — the free-string inconsistency is gone. ⚠ **RENAMED (#1160)**: the `ollama` value was renamed to `self_hosted` (backend-neutral self-hosted slot) — an enum-value **rename** is a breaking (MAJOR) change, shipped as a pre-1.0 exemption. edge_type remains a hard enum that already grew once (#782); enum *widening* is non-breaking, so growth is fine.

## Follow-up candidates

### Bundle 1 — P1: naming + schema-rigor fixes that are breaking after freeze (propose as sub-issue "MCP surface: pre-freeze naming and schema unification")

- ✅ **DONE (#990)** Renamed merge_contexts `source_id`/`target_id` → `source_context_id`/`target_context_id`, resolving the memory/context overload. Old names accepted for one release as a deprecated handler alias (kagura-memory-python-sdk#196 tracks the SDK).
- ✅ **DECIDED (#990): do NOT unify the result-size param.** `k` (relevance top-k), `limit` (list pagination), `cap` (pin-load ceiling) are kept as three distinct conventions — unifying spans ~8 tools (breaking) and erases the semantic signal `k` carries. Frozen and documented as deliberate (see the recall `k` note).
- **P1** Unify time-range bounds on one pair (`from`/`to` or `from`/`until`) across recall_upcoming, analyze_context, setup_connector.
- **P1** Add `format: "uuid"` to every UUID param (analysis bundle, file tools, feedback, set_state, get_state, run_id, file_id, memory_id in feedback).
- **P1** forget: make the two modes explicit (`anyOf`/`oneOf` over memory_id / query) so a bare `{context_id}` call is schema-invalid — destructive tool, ambiguous input.
- ✅ **DONE (#990)** — analyze_context's reserved no-op `query` param was removed (Phase 1; re-add in v1.5), and the internal `model_id` (llm_pricing.id int PK) was **removed from the public MCP surface** (Phase 2 — run uses the server-default model). The **stable `(provider, model)` identifier replacement** is a shared MCP + REST contract (orchestrator `AnalysisParams`, `api/routes/analyses.py`, REST Pydantic schema) and stays deferred to the **v1.5 per-workspace model-selection** redesign — a cross-surface pass, not an MCP-schema-only edit. The REST `model_id` (int) is unchanged for now (out of #990's MCP scope).
- **P1** Decide the `workspace_id` override on the 5 file tools: remove from public schema or document the auth model explicitly.
- ✅ **DONE (#990)** Set `additionalProperties: false` on all 45 inputSchemas (applied centrally in `get_tool_definitions()`; guarded by `tests/mcp_server/test_tool_schema_policy.py`).
- **P1** Unify edge-type param naming/typing: `relation_types`/`edge_types`/`edge_type` → one name, one typing.

### Bundle 2 — P2: enum policy + prose-to-schema hardening (propose as sub-issue "MCP surface: enum extension policy and schema-enforced constraints")

- **P2** Define an enum-extension policy before freeze: connector_type (vendor list — most likely to grow), edge_type (already grew in #782; `neural_association` leaks the algorithm), source_type (`vault` is Obsidian-specific), search_mode. Either commit to additive-enum semver rules or relax to validated strings.
- **P2** Make reranker_provider/reranker_model and embedding_model handling consistent (currently free strings whose descriptions hardcode vendor/model names); rename `bm25_weight` → `keyword_weight` and reconsider exposing `fetch_factor`.
- **P2** Rename setup_connector `litellm_virtual_key_id` → gateway-neutral name.
- **P2** Unify external-identity naming (`external_id` vs `doc_id`) and metadata-bag naming (`context` vs `event_metadata`; consider `metadata` everywhere — also resolves remember's context/context_id/context_summary namespace clash).
- **P2** Unify `report_id` vs `run_id` for background runs; align rollback_sleep_run's name with its param.
- **P2** Move prose constraints into schema: minLength/maxLength for summary/context_summary/key/note, `maxItems: 100` on ingest_events.events, `pattern` for context name and sha256, numeric `minimum`/`maximum` for weights/importance/limits; add explicit `default` values where prose states them.
- **P2** Converge pagination on cursor style (deprecate list_resource_tokens `offset`); flip `include_revoked` default to false for consistency with other list tools.
- **P2** Align recall's optional `context_id` (and feedback/set_state requiredness) with the rest of the surface's "always specify context_id" contract; document the `context_id`/`context_ids` override pattern as the frozen multi-context idiom.
