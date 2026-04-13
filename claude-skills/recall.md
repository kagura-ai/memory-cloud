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

If only one context exists, use it. If multiple, pick the one most relevant to the current project. If unclear, ask the user.

### 2. Search

Use `recall` with the resolved context_id:

```
recall(context_id=..., query="$ARGUMENTS", k=10)
```

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
recall(context_id=..., query="...", k=10, filters={"importance": {"gte": 0.8}})
recall(context_id=..., query="...", k=10, filters={"created_after": "2026-01-01T00:00:00Z"})
recall(context_id=..., query="...", k=10, filters={"source_uri_prefix": "vault://my-vault/"})
recall(context_id=..., query="...", k=10, filters={"source_type": "file"})
```

- `source_uri_prefix`: Filter by origin URI prefix (e.g. `"file://"`, `"vault://my-vault/"`). Useful for querying memories from a specific vault or directory.
- `source_type`: Filter by origin type — `"file"` | `"url"` | `"vault"` | `"api"` | `"manual"`.

Filters can be combined:

```
recall(context_id=..., query="...", k=10, filters={"type": "bug-fix", "tags": ["auth"], "created_after": "2026-03-01T00:00:00Z"})
recall(context_id=..., query="...", k=10, filters={"source_uri_prefix": "vault://", "source_type": "vault", "importance": {"gte": 0.7}})
```

### 3. Display results

Show results in a table: memory_id, summary, type, importance, tags.

### 4. Follow up

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
