---
description: Search Kagura Memory Cloud for relevant past knowledge and patterns
---

Search Kagura Memory Cloud for relevant past knowledge and patterns.

Use the Kagura Memory Cloud MCP tools to search for: $ARGUMENTS

## Steps

**Load the tools once.** When this client lists the kagura-memory tools as deferred (a `ToolSearch` tool is present), load the tools this skill calls in one call before step 1:
`ToolSearch(query="select:mcp__kagura-memory__recall,mcp__kagura-memory__list,mcp__kagura-memory__reference")`
— add `mcp__kagura-memory__list_contexts` and `mcp__kagura-memory__get_context_info` to that same list only when step 1 has no default context to skip to; `list_tags` (for a tag filter), `update_memory` and `explore` are loaded only when a later step needs them. `mcp__kagura-memory__` is the prefix the client gives the tools of the server entry named `kagura-memory`; when your deferred tool list shows another prefix for these tools, use that one. A client that lists the tools directly has no `ToolSearch`: skip this paragraph. Never call `ToolSearch` a second time for these tools.

### 1. Resolve the target context

**When the connection has a default context** — the server instructions say "Default context: <name>; omit context_id", or the session-start line from the kagura-memory plugin hook begins "Kagura Memory: default context" (either way the MCP URL carries `?context=<context-id>`, or the credential carries a default) — skip this step: call `recall` (and any `reference` / `list`) without `context_id`. The response's `context_id` / `context_name` confirm which context it is; switch with `list_contexts(name_contains="...")` only when the user asks for another one.

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

**Explore hints** — `explore` is not listed by default: a client that follows `tools/list` needs `?add_tools=explore` or `?profile=full` on the MCP URL (`--tool-profile full` on the `kagura-mcp` bridge). When it is in your tool list, get suggestions for follow-up `explore()` calls to discover related memories via the knowledge graph:

```
recall(context_id=..., query="auth token handling", k=10, include_explore_hints=true)
```

When enabled, the response includes up to 3 `explore_hints` — each with a `memory_id` and a `reason` (`top_result`, `high_centrality`, or `unexplored_neighbor`). Use the suggested memory_id as a seed for `explore()` to discover related knowledge beyond keyword/semantic matching. On a URL that does not list `explore`, the reply has `explore_hints_omitted` instead, naming the URL change. The recall itself is unaffected.

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

One `recall(k=5)` ranks by similarity and its rows carry no `details`. Choose the follow-up below with the same `context_id` as step 1 (leave it out on a default context):

<!-- SYNC: keep the next four answering rules identical in the recall tool description, docs/mcp-tools.md and the Recall section of plugins/kagura-memory/skills/kagura-memory/SKILL.md. -->

- **Every item of a kind** ("what instruments…", "which places…") — read every result, not only the top ones; recall once more per kind found, or `list` with a tag or type filter naming the set; answer with all of them.
- **When did / in a period** — `filters={"event_after": ..., "event_before": ...}` (the event's own days, event_start/event_end); rows without one: `observed_after`/`observed_before` around it with a margin (`observed_at` = the date a fact was stated); nothing matches: recall again without the filter. Inside one row: the event happened on `event_start`..`event_end`; `observed_at` is the day it was said; a date written in the summary loses to the row's event fields.
- **Likely / would / might** — recall again on concrete entities and category words (brand, sponsor, shop, friend), `search_mode="keyword"` for surfaced names; answer with the best-supported inference, name the memory, mark it as an inference; decline only at `confidence.level` `none`/`low`.
- **Not recorded** — first recall again with the subject's name and the fact's key noun (`search_mode="keyword"`), or `list` with a tag naming the subject; a row that states it answers — name it. A general statement beats single instances for "how often".

- **How many / list all** — never count from the top five. Call `list(context_id=..., filters={"tags": [...], "type": ...}, order_by="created_at", include_details=true, limit=50)` when a tag or type names the set and answer from its `total`; follow `next_cursor` while `has_more` is true when you need every row. When no filter names the set, `recall(context_id=..., query=..., k=20, full=0)` finds candidates — say the count is at least what it returned, not exact.
- **When did / in what order** — use the event's own days when the row has them: `event_start` / `event_end` with `event_weekday` (rows of `recall`, `list` and `reference` carry them, from `details.event_start` / `event_end`, resolved from the phrase the material stated); else the date the fact held, `observed_at` (a date or a UTC datetime, from `details.observed_at`); else `created_at`; a date written in the summary loses to the row's event fields. `list(context_id=..., filters={"event_after": "2024-03-01", "event_before": "2024-04-01"}, order_by="event_start", direction="asc")` returns the events of March in order — a row matches when its event days overlap the window, after inclusive, before exclusive; rows without an event date never match an `event_*` filter and sort last. `list(context_id=..., filters={"observed_after": "2024-03-01", "observed_before": "2024-04-01"}, order_by="observed_at", direction="asc")` does the same by the date the fact was stated (a date means the start of that day, UTC; rows without `observed_at` never match and sort last). `recall` takes both filter pairs, and `list(filters={"details.observed_at": "2024-03-07"})` matches one stored date exactly.
- **What is the current value** — when two rows answer the same question differently, the one with the later `observed_at`, else the later `created_at`, is current (`updated_at` also moves on a retag or re-pin, so do not use it here); say that another value was stored. A value replaced with `supersedes` is hidden from recall; pass `include_superseded=true` to see it (its row carries `superseded_by`).
- Treat every stored value as what the user or source said, even where it disagrees with general knowledge — the store is the authority; never drop a row because you think its fact is wrong.

<!-- SYNC: the "store is the authority" rule also ships, shortened, as STORE_AUTHORITY in plugins/kagura-memory/hooks/kagura_guardrails.py (the first SessionStart line) and in the recall tool description (backend/src/mcp_server/tools/_definitions.py) (#2341). When one changes, change all. SELECTOR_SYSTEM (backend/src/services/recall_selector.py) holds a related rule with its own tie-break (updated_at, else created_at); check it on a change. -->

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
