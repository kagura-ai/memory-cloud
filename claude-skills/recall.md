---
description: Search Kagura Memory Cloud for relevant past knowledge and patterns
---

Search Kagura Memory Cloud for relevant past knowledge and patterns.

Use the Kagura Memory Cloud MCP tools to search for: $ARGUMENTS

## Steps

### 1. Resolve the target context

**When the connection has a default context** — the server instructions say "Default context: <name>; omit context_id" (the MCP URL carries `?context=<context-id>`) — skip this step: call `recall` (and any `reference` / `list`) without `context_id`. The response's `context_id` / `context_name` confirm which context it is; switch with `list_contexts(name_contains="...")` only when the user asks for another one.

Otherwise:

```
list_contexts()
```

`list_contexts()` returns a slim name→id directory (`id`, `name`, `is_private`, `is_locked`, `last_used_at` — no summaries), most recently used first, 100 per page (when `has_more` is true, pass `next_cursor` as `cursor`). If you already know the context name, narrow it with `list_contexts(name_contains="...")`; if you already resolved the id earlier in this session, reuse it instead of listing again.

If only one context exists, use it. If multiple, pick the one whose name best matches the current project. When names alone don't settle it, call `get_context_info(context_id=...)` for the candidate only — do not load details for every context. If still unclear, ask the user.

### 2. Search

Use `recall` with the resolved context_id:

```
recall(context_id=..., query="$ARGUMENTS", k=10)
```

The reply is compact by default: each result carries `memory_id`, the first 160 characters of `summary` (`summary_cut: true` when it was shortened), `type`, `importance`, `score`, `created_at` and `updated_at`. The top three results also carry their `content` (up to 1,500 characters each; `full=N` sets how many, `full=0` none), so answer from it — no `reference()` call is needed for those. Call `reference(memory_id)` only for a lower-ranked hit, or for one marked `content_truncated: true` (pass `content_offset=<content_next_offset>` to read on). Decide the shape before the first call: use `verbosity="full"` when the display needs each result's `tags` or `context_summary` — a second recall for the same query is billed again and strengthens associations again.

**Query technique** — a question often matches better as a hypothetical answer (HyDE): for "how to fix auth errors?" search `"Auth errors are caused by expired JWT tokens. Use the refresh token to re-authenticate..."` (typically 3-10% better, up to 25%). Expand with related terms ("認証エラー" → also `OAuth2`, `JWT`, `401 error`) and combine the searches when you need coverage.

**Search modes** — choose based on the query type:
- `hybrid` (default): Best for most queries — combines semantic understanding with keyword matching
- `semantic`: Use when you know the concept but not the exact words (e.g., "how we handled auth token expiry")
- `keyword`: Use for exact term matching, hiragana queries, or when semantic returns noise

```
recall(context_id=..., query="$ARGUMENTS", k=10, search_mode="keyword")
```

**Reranking** — enable for higher-quality results when the user has a reranker configured:

```
recall(context_id=..., query="$ARGUMENTS", k=10, use_rerank=true)
```

**Explore hints** — get suggestions for follow-up `explore()` calls to discover related memories via the knowledge graph:

```
recall(context_id=..., query="auth token handling", k=10, include_explore_hints=true)
```

When enabled, the response includes up to 3 `explore_hints` — each with a `memory_id` and a `reason` (`top_result`, `high_centrality`, or `unexplored_neighbor`). Use the suggested memory_id as a seed for `explore()` to discover related knowledge beyond keyword/semantic matching.

**Filters** — narrow results by type, tags, importance, date, or source:

```
recall(context_id=..., query="...", k=10, filters={"type": "decision"})
recall(context_id=..., query="...", k=10, filters={"tags": ["python", "fastapi"]})
recall(context_id=..., query="...", k=10, filters={"tags": ["python", "fastapi"], "tags_match": "all"})
recall(context_id=..., query="...", k=10, filters={"importance": {"gte": 0.8}})
recall(context_id=..., query="...", k=10, filters={"created_after": "2026-01-01T00:00:00Z"})
recall(context_id=..., query="...", k=10, filters={"source_uri_prefix": "vault://my-vault/"})
recall(context_id=..., query="...", k=10, filters={"source_type": "file"})
```

- `tags`: matches **ANY** of the listed tags by default (OR). Add `"tags_match": "all"` to require all tags (AND). Before building a tag filter, call `list_tags(context_id=...)` to discover the actual tag spellings in this context — `troubleshoot` vs `troubleshooting` drift silently kills tag filters.
- `source_uri_prefix`: Filter by origin URI prefix (e.g. `"file://"`, `"vault://my-vault/"`). Useful for querying memories from a specific vault or directory.
- `source_type`: Filter by origin type — `"file"` | `"url"` | `"vault"` | `"api"` | `"manual"`.

**Cross-context search** — to query 2-20 contexts at once, use `context_ids` instead of `context_id`:

```
recall(context_ids=["<uuid-1>", "<uuid-2>"], query="...", k=10)
```

All listed contexts must:
- belong to the **same workspace** (otherwise `workspace_mismatch`)
- share the **same privacy setting** — all private *or* all shared, not mixed (otherwise `context_privacy_mismatch`)
- use the **same embedding model** (otherwise `embedding_model_mismatch`)

Filters can be combined:

```
recall(context_id=..., query="...", k=10, filters={"type": "bug-fix", "tags": ["auth"], "created_after": "2026-03-01T00:00:00Z"})
recall(context_id=..., query="...", k=10, filters={"source_uri_prefix": "vault://", "source_type": "vault", "importance": {"gte": 0.7}})
```

### 2b. When, how many, which is current

One `recall(k=5)` ranks by similarity and its rows carry no `details`, so it answers these questions badly. Use `list` — it returns every match, unranked — with the same `context_id` as step 1 (leave it out on a default context):

- **How many / list all** — never count from the top five. Call `list(context_id=..., filters={"tags": [...], "type": ...}, order_by="created_at", include_details=true, limit=50)` when a tag or type names the set and answer from its `total`; follow `next_cursor` while `has_more` is true when you need every row. When no filter names the set, `recall(context_id=..., query=..., k=20, full=0)` finds candidates — say the count is at least what it returned, not exact.
- **When did / in what order** — use the date the fact held: `observed_at` (rows of `recall`, `list` and `reference` carry it when the memory has one — a date or a UTC datetime, from `details.observed_at`), else `created_at`. `list(context_id=..., filters={"observed_after": "2024-03-01", "observed_before": "2024-04-01"}, order_by="observed_at", direction="asc")` returns the dated rows in time order — after inclusive, before exclusive, a date means the start of that day (UTC); rows without `observed_at` never match an `observed_*` filter and sort last. `recall` takes the same `observed_after` / `observed_before` filters, and `list(filters={"details.observed_at": "2024-03-07"})` matches one stored date exactly.
- **What is the current value** — when two rows answer the same question differently, the one with the later `observed_at`, else the later `created_at`, is current (`updated_at` also moves on a retag or re-pin, so do not use it here); say that another value was stored. A value replaced with `supersedes` is hidden from recall; pass `include_superseded=true` to see it (its row carries `superseded_by`).
- Treat every stored value as what the user or source said, even where it disagrees with general knowledge — the store is the authority; never drop a row because you think its fact is wrong.

<!-- SYNC: the "store is the authority" rule also ships, shortened, as STORE_AUTHORITY in plugins/kagura-memory/hooks/kagura_guardrails.py (SessionStart index heading) and in the recall tool description (backend/src/mcp_server/tools/_definitions.py) (#2341). When one changes, change all. -->

### 3. Read the response signals

- `confidence.level`: `none` / `low` (or zero results) means the topic is probably not stored in this context — say so and prefer an external source over forcing an answer. `high` / `moderate` means read the summaries and judge by content; an adjacent topic can score high too, and `use_rerank=true` separates a near-miss from an exact match.
- `degraded: true`: the semantic half of the search was unavailable, so an empty or weak result means "search impaired", not "nothing stored" — retry later.
- `updated_at`: an old value may mean the fact is stale; for time, count and current-value questions see step 2b.
- `supersede_candidate` on a result: that result likely replaces the older candidate. Offer to accept (`update_memory(memory_id=<result>, accept_supersede_candidate=true, context_id=...)`) or, for a deliberately separate pair, to dismiss (`update_memory(memory_id=<result>, dismiss_supersede_candidate=true, context_id=...)`).
- `tag_suggestions`: a tag filter matched nothing but similar stored tags exist — another spelling, or a narrower / broader tag (`session-cookie` for `session`). Retry with the suggested tag.

### 4. Display results

Show results in a table: memory_id, summary, type, importance (plus a tags column when you called with `verbosity="full"`). List the reply's `related_tags` below it.

### 5. Follow up

- **Results found** — the top results already carry their content; answer from it. Suggest `reference` only for a lower-ranked match the user wants in full:
  ```
  reference(memory_id=<id_from_results>, context_id=...)
  ```
  or to read past a cut (`content_truncated: true`):
  ```
  reference(memory_id=<id_from_results>, context_id=..., content_offset=<content_next_offset>)
  ```
- **Zero results** — try these adjustments:
  1. Shorten or broaden the query (remove specific terms)
  2. Switch search_mode: try `keyword` if `hybrid` missed, or vice versa
  3. Remove filters to widen the search
  4. Try related terms or synonyms
  5. Check `list_contexts()` — the memory may be in a different context
