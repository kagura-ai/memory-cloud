---
description: Search Kagura Memory Cloud for relevant past knowledge and patterns
---

Search Kagura Memory Cloud for relevant past knowledge and patterns.

Use the Kagura Memory Cloud MCP tools to search for: $ARGUMENTS

## Steps

### 1. Resolve the target context

```
list_contexts()
```

`list_contexts()` returns a slim name→id directory (`id`, `name`, `is_private`, `is_locked`, `last_used_at` — no summaries), most recently used first. If you already know the context name, narrow it with `list_contexts(name_contains="...")`; if you already resolved the id earlier in this session, reuse it instead of listing again.

If only one context exists, use it. If multiple, pick the one whose name best matches the current project. When names alone don't settle it, call `get_context_info(context_id=...)` for the candidate only — do not load details for every context. If still unclear, ask the user.

### 2. Search

Use `recall` with the resolved context_id:

```
recall(context_id=..., query="$ARGUMENTS", k=10)
```

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

### 3. Read the response signals

- `confidence.level`: `none` / `low` (or zero results) means the topic is probably not stored in this context — say so and prefer an external source over forcing an answer. `high` / `moderate` means read the summaries and judge by content; an adjacent topic can score high too, and `use_rerank=true` separates a near-miss from an exact match.
- `degraded: true`: the semantic half of the search was unavailable, so an empty or weak result means "search impaired", not "nothing stored" — retry later.
- `updated_at`: an old value may mean the fact is stale.
- `supersede_candidate` on a result: that result likely replaces the older candidate. Offer to accept (`create_edge(source_id=<result>, target_id=<candidate>, edge_type="supersedes", context_id=...)`) or, for a deliberately separate pair, to dismiss (`update_memory(memory_id=<result>, dismiss_supersede_candidate=true, context_id=...)`).
- `tag_suggestions`: a tag filter matched nothing but similar stored tags exist — retry with the suggested spelling.

### 4. Display results

Show results in a table: memory_id, summary, type, importance, tags.

### 5. Follow up

- **Results found** — suggest using `reference` for detailed content on the most relevant match:
  ```
  reference(memory_id=<id_from_results>, context_id=...)
  ```
- **Zero results** — try these adjustments:
  1. Shorten or broaden the query (remove specific terms)
  2. Switch search_mode: try `keyword` if `hybrid` missed, or vice versa
  3. Remove filters to widen the search
  4. Try related terms or synonyms
  5. Check `list_contexts()` — the memory may be in a different context
