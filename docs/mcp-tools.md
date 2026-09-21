# MCP Tools Reference

See [MCP Client Setup](mcp-clients.md) for connecting a client, and [Core Concepts](concepts.md) for the memory model behind these tools.

63 tools across 13 categories. Workspace roles: **Owner** > Admin > Member > **Viewer** (read-only). Context roles: **Owner** > Editor > Viewer. Private contexts are visible only to the creator. Members may be restricted to specific contexts via allowlist.

## Tool Profiles

`tools/list` returns all 63 definitions by default. A client that loads every tool schema eagerly pays for the whole list in each session, so the endpoint URL — which the client's local MCP configuration already stores — can ask for fewer:

| Endpoint URL | `tools/list` returns | Approx. size |
|--------------|----------------------|--------------|
| `/mcp/w/{workspace_id}` (or `?profile=full`) | All 63 tools — the default, unchanged | ≈ 82k chars |
| `/mcp/w/{workspace_id}?profile=core` | The 12 core tools: `remember`, `update_memory`, `recall`, `reference`, `recall_upcoming`, `load_pinned`, `forget`, `explore`, `get_context_info`, `list_contexts`, `list_tags`, `feedback` | ≈ 28k chars (about 65% smaller) |
| `/mcp/w/{workspace_id}?tools=remember,recall,reference` | Exactly the named tools — an explicit allowlist, wins over `profile` | ≈ 14k chars for these three |

Sizes are the compact JSON of the `tools` array, measured at v0.73.0 (the descriptions were trimmed in that release; at v0.72.0 the same lists were ≈ 111k / 45k / 23k). Per-client instructions: [MCP Client Setup › List fewer tools](mcp-clients.md#list-fewer-tools).

- Tool names are comma-separated and case-sensitive; surrounding whitespace is trimmed, duplicates collapse, and at most 100 names are read. The result is always in registry order, whatever order the URL uses.
- Unknown names are ignored (and logged by the server), so a URL keeps working if a tool is later renamed or removed. If **no** name matches, or `profile` is anything other than `full` / `core`, `tools/list` fails with JSON-RPC `-32602` (invalid params) and a message naming the valid values.
- Both transports honour the parameters — session-based Streamable HTTP and stateless MCP 2026-07-28 — on `/mcp` as well as `/mcp/w/{workspace_id}`.

> **A profile is a view, not an authorization boundary.** It filters `tools/list` and nothing else. `tools/call` never reads it: a tool left out of the list stays callable by anyone whose role allows it. To restrict what a key can do, use workspace and context roles.

## Memory (7)

| Tool | Description | Required Role |
|------|------------|---------------|
| `remember` | Store a new memory (summary + content + type; optional `delivery_mode`) | Member+ |
| `recall` | Search memories with Hybrid Search (supports `trust_tier` filter). Results are Layers 1-2; `related_tags` is `[{tag, count}]` | Viewer+ |
| `recall_nearby` | Deterministic WHERE-axis query — memories with `details.location` within `radius_m` of a point, nearest first | Viewer+ |
| `reference` | Get full 3-layer details of a memory | Viewer+ |
| `update_memory` | Update an existing memory in-place or upsert by external ID | Member+ |
| `forget` | Soft-delete a memory (retention bounded by the deployment's cleanup window, default 30 days) | Member+ |
| `explore` | Discover related memories via Neural Memory graph | Viewer+ |

> **Response format.** Every tool returns one JSON text block, serialized as compact UTF-8 — non-ASCII text (e.g. Japanese) arrives as-is, never as `\uXXXX` escapes, because the calling model pays for every character. Fields that are empty on most results are omitted rather than sent as `null` / `[]`: a `recall` result carries `context_summary`, `superseded_by`, `contradicts` and `supersede_candidate` only when they have a value, and `score` is rounded to 4 decimals. Treat an absent key as "none". The authoritative per-tool shape is the `Returns:` line of each tool description (`tools/list`).

## Agent Substrate (7)

The primitives an autonomous agent loop needs beyond a knowledge store — see [Concepts › Agent Memory Substrate](concepts.md#agent-memory-substrate).

| Tool | Description | Required Role |
|------|------------|---------------|
| `load_pinned` | Deterministically load always-load memories (`delivery_mode="always"`) — Goal / Guardrail / policy | Viewer+ |
| `recall_upcoming` | List upcoming Time Memories (`type="time"`, `delivery_mode="on_trigger"`). Items are `{memory_id, summary, type, trigger}`; `include_details=true` returns the full `details` instead of `trigger` | Viewer+ |
| `set_state` | Upsert agent scratch state (key→value, optional TTL; excluded from recall) | Editor+ |
| `get_state` | Read one state key, or list all live state for a context | Viewer+ |
| `record_measurement` | Append one numeric observation to a metric's series (HOW-MUCH lane; excluded from recall, untouched by Sleep) | Editor+ |
| `recall_series` | Read a metric's series bucketed by day/week/month with avg/min/max/sum/count/last | Viewer+ |
| `feedback` | Record whether a recalled memory was helpful (append-only signal) | Viewer+ |

## Agent Control Plane (10, preview)

The v0.49.0 control plane builds on existing workspace RBAC: agents are registry resources, not principals, and context bindings can only remove access from an agent-bound member key. Registry, bindings, composed bootstrap, W3C Trace Context/baggage correlation, and the append-only audit foundation are implemented.

| Tool | Description | Required Role |
|------|------------|---------------|
| `register_agent` | Register a workspace-scoped agent | Owner/Admin |
| `list_agents` | List registered agents and lifecycle/enforcement status | Owner/Admin |
| `get_agent` | Get one registered agent | Owner/Admin |
| `update_agent` | Update metadata, `status`, or `enforcement_mode` | Owner/Admin |
| `delete_agent` | Permanently delete an agent and its bound keys; prefer `status="retired"` operationally | Owner/Admin |
| `bind_agent_context` | Add a purely subtractive context binding | Owner/Admin |
| `list_agent_bindings` | List an agent's context bindings | Owner/Admin |
| `update_agent_binding` | Update read/write/default binding policy | Owner/Admin |
| `unbind_agent_context` | Remove a binding (default-deny in enforce mode) | Owner/Admin |
| `get_agent_bootstrap` | Compose context guide + pinned + optional trusted recall + upcoming + state for session start | Agent-bound key or Owner/Admin |

> **Preview boundary:** per-memory type/source filters are enforced on the memory-read lanes (recall, recall_nearby, reference, forget, explore, load_pinned, upcoming) for enforce-mode agents as of [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299) — and on the enumeration/aggregate surfaces (list, stats, access-patterns, get_cluster) as of [#1301](https://github.com/kagura-ai/memory-cloud/issues/1301) — `null` = all types, `[]` = deny-all; shadow mode records `would_deny` without filtering. `traceparent` plus W3C baggage correlation for `agent_id` / `session_id` / `run_id` is implemented, but server-side span export remains out of scope for P0. `memory_access_events` is live for bootstrap, load-pinned, feedback, recall, reference, remember, update, and forget with binding deny / `would_deny` persistence.

## Neural Edges (4)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_edges` | List edges connected to a memory | Viewer+ |
| `create_edge` | Create an edge between two memories | Member+ |
| `update_edge` | Update edge weight or type | Member+ |
| `delete_edge` | Delete an edge between two memories | Member+ |

## Contexts (7)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_context_info` | Get context metadata and guidelines | Viewer+ |
| `list_contexts` | Slim name→id directory of the contexts you can access, most recently used first (details are opt-in — see below) | Viewer+ |
| `create_context` | Create a new context | Owner/Admin |
| `update_context` | Update context settings (summary, usage guide, resource_id, is_public) | Editor+ |
| `delete_context` | Delete a context and all its memories | Owner/Admin |
| `merge_contexts` | Merge memories from source context into target context | Owner/Admin |
| `update_search_config` | Tune hybrid search weights, reranker settings, and query-intent routing (`routing_mode`) per context | Editor+ |

### `list_contexts` response shape

`list_contexts()` exists to turn a context **name** into an **id**, so by default each item is just `{id, name, is_private, is_locked, last_used_at}` — no `summary`, no `embedding_model`. A workspace with dozens of contexts stays a few thousand characters instead of overflowing an MCP client's tool-result limit. For one context's full summary, usage guide and search config call `get_context_info(context_id)`.

| Parameter | Effect |
|-----------|--------|
| `name_contains` | Case-insensitive substring match on the context name or display name (trimmed, ≤100 characters; blank = no filter). No match is a success with an empty list. |
| `include_stats` | Adds `memory_count` per item. |
| `include_summary` | Adds `summary` truncated to 300 characters; items that were cut also carry `summary_truncated: true` (a null summary stays null). |
| `include_details` | Adds the full `summary` (up to 2,000 characters each) and `embedding_model` — the previous default item shape ([#1600](https://github.com/kagura-ai/memory-cloud/issues/1600)). Wins over `include_summary`; combine it with `name_contains`. |

Envelope: `{status, contexts, count, total, limit, can_create}`. `count` is the number of contexts in the workspace (quota usage against `limit`; it can exceed what you are allowed to see and is not affected by `name_contains`), `total` is the number of contexts in this response. A non-boolean flag or an over-long `name_contains` returns a `validation_error`; an explicit `null` for any parameter is treated as omitted.

## Tags (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_tags` | List tag vocabulary in a context (call before remember/recall to align tagging) | Viewer+ |

## Files / R2 Attachments (5)

| Tool | Description | Required Role |
|------|------------|---------------|
| `init_file_upload` | Reserve quota + return presigned PUT URL (R2, ≤100 MiB) | Member+ |
| `complete_file_upload` | Finalize upload after R2 PUT, verify sha256, mark as uploaded | Member+ |
| `list_files` | List uploaded, non-deleted files in the workspace (newest first) | Viewer+ |
| `get_file_download_url` | Issue presigned GET URL for a file | Viewer+ |
| `delete_file` | Soft-delete a file object | Member+ |

## Analyses — Memory Analysis (5)

Cluster memories into themes (kouchou-ai-style UMAP + KMeans + LLM labeling) for large-scale qualitative analysis.

| Tool | Description | Required Role |
|------|------------|---------------|
| `analyze_context` | Start an analysis run (or preview cost with `dry_run=true`) | Owner + Pro plan + BYOK + quota |
| `list_analyses` | List past analysis runs for a context | Owner |
| `get_analysis` | Get a completed analysis (clusters, labels, stats) | Owner |
| `get_active_analysis` | Get the in-flight analysis for a context (if any) | Owner |
| `get_cluster` | Drill into a single cluster's member memories | Owner |

Analysis runs are capped by `ANALYSIS_MAX_MEMORY_COUNT` (default 10,000; preview and start reject larger contexts). Since v0.47.0, cancellation is all-or-nothing, deleted-context runs are invisible on both REST and MCP, strict BYOK labeling never falls back to the platform key, and runs fail when more than half of labelable clusters fail labeling.

## Resources — External Data Ingestion (6)

| Tool | Description | Required Role |
|------|------------|---------------|
| `setup_resource` | Create public context + issue resource token | Owner/Admin + plan with the `resources` feature (XL) |
| `setup_connector` | Provision an ai-worker chat connector (resource + connector row + token) | Owner/Admin + plan with the `connectors` feature (XL); connector seat cap applies second |
| `list_resource_tokens` | List active resource tokens for your workspace | Owner/Admin |
| `ingest_events` | Batch upsert/delete events into a resource (max 100 events; session-auth MCP variant) | Member+ |
| `get_resource_impact` | Resource stats (tokens, memories, schema version) | Viewer+ |
| `get_resource_schema` | Field definitions for a resource | Viewer+ |

The `resources` / `connectors` feature gates (#1551) apply to **new creation only**: resources, resource tokens and connectors that already exist on a lower plan keep working — they stay listed, served, editable and ingestible (a resource token's `quota_events_per_hour` change is still bounded by the current tier's aggregate ceiling — see the resource-tokens guide). The per-plan token / connector caps bound only what a lower plan already holds; for plans with the feature they are the second gate at creation time.

## Secrets (5)

Zero-knowledge secret store: the server holds only `age` public recipient keys and opaque ciphertext, and **never decrypts**. Encryption/decryption happen client-side (the `kagura secret` CLI / SDK). `list` returns metadata only — there is no endpoint that returns a plaintext value.

| Tool | Description | Required Role |
|------|------------|---------------|
| `secret_register_pubkey` | Register your own `age` recipient public key (starts pending; an owner approves it before it can receive grants) | Member+ |
| `secret_put` | Store age-encrypted ciphertext + grant approved recipients (`recipients_snapshot` must match `grant_pubkey_ids`) | Owner/Admin |
| `secret_get` | Fetch ciphertext you hold an active grant for (decrypt locally; every fetch is recorded in a tamper-evident audit log) | Member+ |
| `secret_list` | List secret names + metadata (status, version, grant count, rotation flag) — never values | Owner/Admin |
| `secret_revoke_grant` | Revoke a recipient's grant and flag the secret `rotation_needed` (not retroactive — rotate upstream) | Owner/Admin |

## Sleep Maintenance (3)

Background consolidation of memories (decay, edge pruning, theme summarization).

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_sleep_history` | List past sleep runs | Viewer+ |
| `get_sleep_report` | Detailed sleep report with all actions | Viewer+ |
| `rollback_sleep_run` | Rollback all actions from a completed sleep run | Member+ (report owner only) |

## Usage (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_usage` | Get current workspace usage (memories, contexts, members, MCP calls/day) | Viewer+ |

## API-Key Bindings (2)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_my_bindings` | List your public-bound API keys (read-only; owner-scoped) | Viewer+ |
| `describe_binding` | Describe one binding by `key_id` XOR `context_id` (read-only; owner-scoped) | Viewer+ |

## Usage notes

The descriptions an agent receives from `tools/list` are paid for on every session, so they carry only what is needed to call a tool correctly: its purpose, when to use it instead of a neighbour, what each parameter means, the response keys, and the rules that must not be missed. The walkthroughs, rationale and longer examples live here. The plugin's `guide` skill carries the short version for an agent that wants it in-session.

### `recall`

**Which read tool.** `recall(query)` finds candidate memories and returns Layers 1-2 (summary + context summary). `reference(memory_id)` returns one memory in full (Layer 3). `explore(memory_id)` walks the graph to adjacent memories. `load_pinned()` returns the complete pinned set, unranked. `recall_upcoming()` and `recall_nearby()` are deterministic time / place queries, not search. The usual flow is recall → reference → explore; for a bug fix: `recall("<error message>")` → find similar past fixes → `reference()` for the details → apply.

**Search with a hypothetical answer (HyDE).** For question-style queries, generate the answer you would expect to find and search with that instead — typically 3-10% better results, up to 25% in the best cases, because a stored memory reads like an answer, not like a question.

- EN: "How to fix auth errors?" → search `"Auth errors are caused by expired JWT tokens. Use the refresh token to re-authenticate..."`
- JA: 「認証エラーの対処法」 → search `"認証エラーはJWTトークン期限切れが原因。リフレッシュトークンで再認証する..."`

**Expand the query.** Run the same question with related terms (「認証エラー」 → also `OAuth2`, `JWT`, `401 error`) and combine the result sets when you need coverage rather than one best hit.

**Filters.**

```
filters={"type": "code"}
filters={"tags": ["python", "fastapi"]}                       # ANY of the tags (exact match)
filters={"tags": ["python", "fastapi"], "tags_match": "all"}  # ALL of the tags
filters={"importance": {"gte": 0.8}, "scope": "persistent"}
filters={"created_after": "2026-03-01T00:00:00Z"}             # also created_before / updated_after / updated_before
filters={"source_uri_prefix": "vault://", "source_type": "vault"}
filters={"trust_tier": "trusted"}
filters={"near": {"lat": 35.68, "lon": 139.77, "radius_m": 500}}       # details.location within the radius
filters={"within": {"polygon": [{"lat": 35.6, "lon": 139.7}, ...]}}  # geofence, 3-128 vertices, ring auto-closed
```

- **Tag drift.** `tags_normalize: true` also matches stored spellings that differ only mechanically from yours — case, hyphen / underscore / space, simple plural — so `dev-environment` matches `Dev_Environment`. It does not match abbreviations (`dev-env`): when a tag filter returns nothing and similar tags exist, the response carries `tag_suggestions` (`{requested_tag: ["stored-tag (count)", ...]}`), so an empty result tells you whether the topic is missing or just spelled differently. Tags of the same shape that differ from yours only in the values of their numbers are never suggested — `issue:#179` is a different identifier from `issue:#1599`, not another spelling of it. The filter itself is never widened. `list_tags` is the way to avoid the problem up front.
- **`trust_tier: "trusted"`** excludes external / connector-ingested memories. It is opt-in — a default recall returns them. Pass it on reads that decide what the agent does next, so untrusted content cannot act as an instruction (OWASP LLM01 / LLM03).
- **Geo.** `near` defaults to a 1000 m radius, clamped to 1 m-1000 km; a malformed `near` is a `validation_error`, and memories without a location never match. `within` composes with `near` (AND).

**Few or no results.** Shorten the query, remove filters, try related terms, lower the importance threshold, or switch to `search_mode="keyword"`.

**Search modes.** `hybrid` (default) combines semantic understanding with keyword matching (60% semantic + 40% BM25 unless the context's search config says otherwise) and applies Neural Memory boosting. `semantic` is vector similarity only — best when you know the concept but not the words; Neural Memory is skipped. `keyword` is BM25 only, with no embeddings — best for exact terms and for Japanese hiragana-only queries, where embedding models struggle.

**`confidence`.** A cheap triage hint for "is anything relevant here, or should I go external?" — not a correctness verdict. `level` (`high` / `moderate` / `low` / `none`) is driven by `top_score`, the best hit's absolute semantic cosine, and `prominence`, `(top_score − mean background cosine) / mean background cosine` — a ratio, so it is robust to an embedding model's cosine scale rather than a global cutoff.

- `none` / `low` (or `count` 0): likely nothing relevant is stored in this context. Prefer an external source over forcing an answer out of the results. This signal is reliable even without a reranker.
- `high` / `moderate`: relevant memory is likely present, so read the summaries and judge from their content. `level` measures topical match strength, so a closely related near-miss can also read `high`; it does not guarantee the exact fact is stored. To separate a near-miss from an exact match pass `use_rerank=true` (a cross-encoder) — plain cosine cannot.
- `relative_margin` is kept for transparency but inflates on off-topic queries; do not use it to decide relevance.
- Examples: `{"level": "high", "top_score": 0.92, "prominence": 0.55, "result_count": 20}`; an empty pool returns `{"level": "none", "top_score": null, "result_count": 0}`.

**`degraded: true`** means the semantic half of the search was unavailable (`degraded_reason` says which), so the result set is keyword-only and `confidence` was computed on a different basis. A degraded empty or low result means "the search was impaired", not "nothing is stored": retry later rather than concluding absence. A healthy search carries neither key.

**`updated_at`** on each result is the last time that fact changed (null if never edited since creation) — a staleness cue that costs no extra call.

**`supersede_candidate`.** At ingest the server checks whether a new memory's nearest neighbour is a near-identical earlier fact — i.e. whether the new memory reads like the updated version of it. If so, the newer memory carries `supersede_candidate: {memory_id, summary, similarity, detected_at}` (the candidate is the *older* fact) on `recall` and `reference`. It is a suggestion, never an auto-applied edge, and it is liveness-guarded: you only ever see a candidate you can already read.

- Accept: `create_edge(source_id=<the memory carrying it>, target_id=<supersede_candidate.memory_id>, edge_type="supersedes", context_id=...)`. The stale candidate is shadowed out of default recall — the strongest lever for update-correctness — and the server clears the suggestion.
- Reject: `update_memory(memory_id=<the memory carrying it>, dismiss_supersede_candidate=true, context_id=...)` when the two are deliberately separate (for example the same conclusion recorded at two altitudes). Neither memory is deleted or shadowed; only the suggestion is tombstoned. Detection resumes for the pair if their similarity later changes materially, and a suggestion for a different memory still surfaces.
- Ignore: it disappears by itself once accepted or once the candidate is deleted.

**`explore_hints`.** `include_explore_hints=true` adds up to three seed memories for a follow-up `explore()` — reason `top_result`, `high_centrality` or `unexplored_neighbor`. They bridge recall (precision search) and explore (graph discovery) without mixing their scoring; use them when the user asks "what else is related?".

**`include_superseded`** returns memories shadowed by a `supersedes` edge, annotated with `superseded_by`, for audit and history ([#1208](https://github.com/kagura-ai/memory-cloud/issues/1208)). **Cross-context recall** (`context_ids`, 2-20 contexts, [#81](https://github.com/kagura-ai/memory-cloud/issues/81)) requires one workspace, one privacy setting and one embedding model across the list.

### `remember`

**Three layers.** `summary` (10-500 characters, what recall matches) → `context_summary` (why this matters and how to use it) → `content` / `details` (the complete data, code or structured information).

**Write the summary as the reusable conclusion, not the process**, and include the synonyms a later search would use.

- Good: "Database performance: PostgreSQL JSONB GIN index optimization for faster queries"
- Good: "JWT expiry caused 401. Fixed with refresh token rotation and clock skew handling."
- Bad: "Discussed auth errors in today's meeting."
- Bad: "JSONB index optimization" — too narrow; it will not match "database performance".

**Chunk long documents by meaning.** The best summary length is 100-250 characters (max 500). For anything over ~2,000 characters create several memories, one per topic — "User authentication module", "Database models", "API routes" — never "Document part 1/3". Link the pieces with common tags or `context={"parent_doc_id": "...", "section": "intro"}`, and carry 50-100 characters of overlap from the adjacent section in `context_summary`.

```
Bad:  remember(summary="auth.py file", content=<entire 5000-line file>)
Good: remember(summary="OAuth2 login implementation",  content=<login function>,      tags=["auth", "oauth2"])
      remember(summary="JWT token validation logic",   content=<validation function>, tags=["auth", "jwt"])
      remember(summary="Session management utilities", content=<session helpers>,     tags=["auth", "session"])
```

**Enrich for search quality.** Tags: key entities and topics (`["OAuth2", "FastAPI", "Python"]`), plus category tags (`category:auth`, `category:料理`); for Japanese include kanji, katakana and hiragana variants (`["鯖", "サバ", "さば"]`). Call `list_tags` first and reuse stored spellings. Importance: critical 0.9-1.0, useful 0.6-0.8, reference 0.3-0.5. Type: a consistent vocabulary (`decision`, `pattern`, `bug-fix`, `troubleshooting`, `learning`, `note`, `code`). Context: background, related issues, reasoning. The more semantic metadata a memory carries, the better it ranks.

**Several domains in one context.** Use domain tags (`domain:personal`, `domain:work`), mark visibility in `context` (`{"visibility": "private", "shareable": false}`), and filter on recall with `filters={"tags": ["domain:work"]}`.

**Updating a fact — `supersedes`.** When you store the newer version of something already remembered, pass `supersedes=<old_memory_id>`. The old memory is shadowed out of default recall — not deleted: it stays reachable via `recall(include_superseded=true)` and `explore()`, and deleting the `supersedes` edge restores it. Prefer this to a near-duplicate: a duplicate leaves the stale and the fresh fact competing in recall, whereas a declared supersession makes the update authoritative. If you did not set it at write time, the server's near-duplicate detection surfaces a `supersede_candidate` later (see `recall` above).

**Durability — what `scope="working"` means.** A new memory is committed to the database before the call returns. It is saved; do not re-write it or wait. `scope` selects the consolidation lifecycle, not whether it was stored:

- `working` (the default for a normal write) — a nightly consolidation pass can promote it to `persistent`; `persistence.promotes_via` names the pass this server actually runs (null if none is enabled). That pass will not archive a working memory younger than `persistence.consolidation_archive_min_age_days`, and only one that has never been adopted.
- `persistent` — outside consolidation's reach. `delivery_mode="always"` pins straight here on write; that is a delivery guarantee, not a stronger durability guarantee than a working-scope write already has.

The age floor is scoped to consolidation and is not a retention SLA: separate near-duplicate merge maintenance can retire an unpinned memory at any age (its tags and edges move to the memory it merged into; `delivery_mode="always"` memories never enter that pass), and `forget()` removes one on demand. The response carries a `persistence` block for the scope you actually got.

**Write lint.** `lint: [{code, hint, subject?}]` appears only when something about the write will hurt future recall — `summary_short`, `summary_long`, `summary_narrative`, `no_tags`, `tag_near_duplicate` (a tag that near-duplicates one already in the context). A near-duplicate is a mechanical variant, a prefix abbreviation (`dev-env` / `dev-environment`) or a typo within two edits — never two tags of the same shape that differ only in the values of their numbers, so a new `issue:#1599`, `v0.73.0` or `session-2026-09-21` is not flagged against other issue, version or date tags written the same way (a different count of numbers — `v0.73` / `v0.73.0`, `session-2026-09` / `session-2026-09-21` — or the same number padded differently — `sprint-07` / `sprint-7` — still goes through the prefix and typo rules); `tag_suggestions` on `recall` uses the same relation. A clean write has no `lint` key. It is advisory: the memory is already stored, and acting on a hint means calling `update_memory()`.

**Never store secrets.** No API keys, tokens, passwords or client secrets; no private keys or certificates; no personally identifiable information; no OAuth refresh tokens or session cookies; no contents of `.env` files or environment variables with credentials. If the input contains such data, the agent refuses and asks for redaction first. The one exception is location ([#1331](https://github.com/kagura-ai/memory-cloud/issues/1331)): geographic coordinates in `details.location = {lat, lon, label?, text?}` are a first-class payload (the WHERE axis), stored deliberately when the user wants a memory tied to a place — `lat` / `lon` as JSON numbers, validated server-side, queryable via `recall_nearby`. Put coordinates only there, never in `context`, which is replicated into the search index's payload store.

The embedding is generated asynchronously after `remember` returns, so a new memory is not findable via `recall()` for a brief moment.

### `update_memory`

- **In place (`memory_id`)** keeps the memory ID, graph edges and creation timestamp, and re-embeds only when `summary`, `context_summary` or `content` changed (`re_embedded`). Use it when you hold a `memory_id` from `recall()`.
- **Upsert (`external_id`)** looks the memory up by `details.resource_id` within the context, for sync workflows with stable external identifiers. Not found → `operation: "created"`. Found → a new memory is written first and the old one soft-deleted, so the response carries a new `memory_id` and `operation: "replaced"`. Requires `summary`, `content` and `type`.
- `details` is replaced wholesale: resend `location` when you update `details`, or it is dropped.
- `delivery_mode="always"` pins, `"on_recall"` unpins (the memory stays persistent).

### `reference`

1. `recall()` to find relevant memories. 2. Read the summaries and pick the interesting ones. 3. `reference()` for the full content, structured context, provenance (`source_uri`, `source_type`, `client`) and declared links of each. 4. Present the complete picture.

### `forget`

Always verify before deleting: show the memory's summary, warn when `importance > 0.8`, and get explicit approval. For bulk deletion: `recall()` to find candidates → review the list with the user → confirm → loop `forget(memory_id)`; query mode (top-k matches of a query) is for cleanup where that review is not needed. A target that was already deleted, has a wrong ID, or belongs to someone else is skipped silently — `deleted_count` is 0 — so check the ID with `recall()`. Deletion is soft; retention is bounded by the deployment's cleanup window (`CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`, default 30 days), and the memory's graph edges are cleaned up with it.

### `explore`

| Parameter | Guidance |
|-----------|----------|
| `depth` | 1 = direct connections, 2 = recommended default, 3+ = broader but slower and less relevant (max 5) |
| `min_weight` | Typical edge weights are 0.02-0.05. `0.0` = all connections (good for a first look), `0.05` = most connections (default), `0.1` = stronger only, `0.3`+ = very strong only, may return nothing |
| `relation_types` | `neural_association` (Hebbian, automatic), `related_to`, `depends_on`, `learned_from`, `continues_from`, `references_file` (producer-asserted structural edges). Omit to follow every type |

`metadata.total_activated` is the number of nodes the traversal reached and `returned` the number left after `min_weight` filtering. `returned = 0` with `total_activated > 0` means the threshold is too high — lower `min_weight` to 0.0-0.05. Results are ranked by activation strength (graph-based relevance), top 10.

### `list_tags`

Tag filters match exactly, so semantically identical tags under different spellings (`troubleshoot` / `troubleshooting` / `trouble-shoot`) silently erode `recall(filters={"tags": [...]})` over time. `list_tags` is the primary mitigation: call it before `remember()` to reuse existing spellings, and before a tag-filtered `recall()` to build filters that match what is stored.

```
list_tags(context_id="...")                   # top 50 tags by usage count
list_tags(context_id="...", min_count=5)      # only frequently used tags
list_tags(context_id="...", prefix="auth")    # autocomplete: tags starting with "auth"
list_tags(context_id="...", sort="recent")    # most recently used first
list_tags(context_id="...", sort="alpha")     # alphabetical, case-folded
```

An empty context returns `tags: []` and `total: 0`, not an error. Soft-deleted memories are excluded and the workspace boundary is honoured for shared contexts. `prefix` escapes `%` and `_`, so it cannot be used as a wildcard probe.

### Edges

| `edge_type` | Meaning |
|-------------|---------|
| `related_to` (default) | General relationship |
| `depends_on` | Target depends on source |
| `learned_from` | Knowledge derived from source |
| `neural_association` | Created automatically by Hebbian learning — prefer `related_to` for manual edges |
| `continues_from` | Chronological / narrative successor between chat memories (producer-asserted, directional; [#782](https://github.com/kagura-ai/memory-cloud/issues/782)) |
| `references_file` | Structural reference from a chat memory to a file overview (producer-asserted, directional; #782) |
| `supersedes` | Source is the newer memory, target the outdated one: the target is shadowed out of default recall while the source lives ([#1208](https://github.com/kagura-ai/memory-cloud/issues/1208)). Also the way to accept a `supersede_candidate` |
| `contradicts` | Both sides stay visible, annotated — contradiction never hides |

Weights run from 0.0 to 3.0; the default 1.0 is a full-confidence manual edge, so `create_edge` usually needs only `source_id` and `target_id`.

**Calling `create_edge` on an existing pair** ([#1321](https://github.com/kagura-ai/memory-cloud/issues/1321)). The (source, target) pair is unique per user, so the outcome is deterministic. An existing auto edge (origin `hebbian` / `semantic`) takes your values → `operation: "updated"` plus a `previous` pre-image; a hebbian edge is promoted to origin `declared`, a semantic edge keeps origin `semantic`. An existing declared edge with identical values is left alone → `operation: "unchanged"`, safe to retry. An existing declared edge with different values is rejected with `edge_exists`, because declared links are provenance — use `update_edge`, or pass `overwrite=true` to re-assert.

`update_edge` leaves an omitted `weight` unchanged (there is no default), so a type-only update is `update_edge(..., edge_type=...)`. `delete_edge` is a hard delete; if the memories are still co-accessed, Hebbian learning may recreate a `neural_association` edge. A typical cleanup is `explore()` → `list_edges()` → `delete_edge()`.

### `get_context_info`

Call it at session start and after switching contexts. `context.usage_guide` holds the context-specific rules and takes precedence over generic defaults; `context.summary` says what the context is for; `context.is_private` tells you whether workspace members can see what you write; `instructions` is the general quick reference for the memory tools. `stats` always carries the totals, and `stats.details` (by type, by importance, last 7 days) unless `include_details=false`.

### `update_search_config`

- More keyword matching: `semantic_weight=0.5, bm25_weight=0.5`. Semantic-heavy: `semantic_weight=0.7, bm25_weight=0.3`. The two must sum to 1.0.
- Reranking: `use_rerank=true, reranker_provider="voyage"` (or `"cohere"`) needs the provider's API key; `reranker_provider="self_hosted"` is keyless and uses the deployment's local reranker, so `reranker_model` may be omitted. A `recall` that omits `use_rerank` follows this setting.
- `reinforce_enabled` is the bounded adoption + feedback re-rank. Contexts created since [#1207](https://github.com/kagura-ai/memory-cloud/issues/1207) start enabled; older ones keep their stored setting (typically false) until you set it. It never overrides semantic relevance: each score is multiplied by a factor in `[1 − reinforce_max_boost, 1 + reinforce_max_boost]`.
- `routing_mode` ([#1212](https://github.com/kagura-ai/memory-cloud/issues/1212)): turn on `log_only` first to measure the traffic mix with zero ranking change, then `active`.

### Sleep Maintenance

`rollback_sleep_run` reverses each recorded action in order: `create_edge` → the edge is deleted; `merge` → the soft-deleted loser memory is restored and re-embedded; `update_importance` → the previous value is restored; `promote` → scope goes back to `working`; `archive` → the memory is restored and re-embedded. It works on reports with status `completed` or `degraded` — a degraded run (partial judge-LLM failures, [#1183](https://github.com/kagura-ai/memory-cloud/issues/1183)) still executed real merges — and marks the report `rolled_back`. Reports created before action recording existed have nothing to roll back. `merges_unreversible` ([#1450](https://github.com/kagura-ai/memory-cloud/issues/1450)) counts shadow merges that were not reversed because a later writer changed or removed the edge; restoring the pre-merge state would have discarded that newer state, so the run reports `partial_rollback`.

### Files

Uploads go to platform-managed object storage (Cloudflare R2) in three steps: `init_file_upload` reserves quota atomically and returns a presigned PUT URL; the client PUTs the bytes; `complete_file_upload` verifies the stored object against the declared sha256 and size and moves the file from `reserved` to `uploaded`. The per-file cap is 100 MiB. Sending the same sha256 twice in one workspace returns a `conflict` that names the existing `file_id`. `delete_file` releases the quota immediately; the binary lingers for 7 days before the nightly sweeper removes it, with no client-visible effect.

### Resources and connectors

```
setup_resource(name="ec-products", resource_id="ec_products")
  → context created, token issued, ready for ingest_events()

setup_connector(connector_type="slack", resource_id="slack_general")
  → connector created, token issued; use idempotency_key="{connector_id}:..."

ingest_events(resource_id="ec_products", events=[
  {"op": "upsert", "doc_id": "PROD-1", "version": 1, "payload": {"name": "...", "price": 5980}},
  {"op": "delete", "doc_id": "PROD-999"}
])

get_resource_impact(resource_id="ec_products")   → {token_count: 2, memory_count: 500, current_schema_version: 3}
get_resource_schema(resource_id="ec_products")   → {schema_version: 3, field_definitions: [{name: "product_name", ...}]}
list_resource_tokens(resource_id="ec_products")  → {tokens: [{id: 1, resource_id: ..., is_active: true, ...}], total: 3}
```

`setup_connector` creates a resource, a connector row and a connector-scoped resource token in one operation; its `runtime` object is validated server-side (the schema in `tools/list` is advisory).

### Analyses

```
analyze_context(context_id="...", dry_run=True)   # cost preview, nothing is created
analyze_context(context_id="...")                 # starts the run → run_id
get_analysis(run_id="...")                        # poll until finished_at is set
list_analyses(context_id="...", limit=20)         # → {items: [...], next_cursor: "2026-04-30T12:34:56"}
get_cluster(run_id="...", cluster_index=3)        # label, representatives, paginated members
```

`get_analysis` answers `run_not_found` both for unknown ids and for runs in another workspace, so existence is not leaked.
