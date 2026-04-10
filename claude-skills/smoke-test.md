---
description: Run comprehensive smoke test of all MCP tools via live MCP connection
---

Verify MCP tools work correctly by executing them in sequence against temporary test contexts.
Tests 21 of 24 tools (excludes 3 sleep tools that require a prior Sleep Maintenance run).
Use this after deployments, tool description changes, or MCP server updates.

**Prerequisite:** MCP server must be running and connected.

## Steps

### 1. Preparation

Create a temporary test context for isolation:

```
list_contexts()
-> Verify: returns a list with count >= 0

create_context(name="smoke-test-{unix_timestamp}", description="Temporary context for MCP smoke test. Safe to delete.")
-> Verify: returns context with id (UUID format)
-> Save returned context_id for all subsequent steps
```

### 2. Context tools

```
get_context_info(context_id=..., include_details=true)
-> Verify: returns context.name matching "smoke-test-*", context.id matches

update_context(context_id=..., display_name="Smoke Test", summary="Temporary smoke test context")
-> Verify: success response

update_search_config(context_id=..., semantic_weight=0.6, bm25_weight=0.4)
-> Verify: success response
```

### 3. Memory write tools

```
remember(
  context_id=...,
  summary="MCP smoke test memory — testing remember tool",
  content="This is a test memory created by smoke-test. If you see this, the remember tool is working correctly.",
  type="note",
  importance=0.5,
  tags=["smoke-test", "automated"],
  context_summary="Created during automated MCP smoke test for verification purposes."
)
-> Verify: returns memory with id (UUID format)
-> Save returned memory_id
```

### 4. Memory read tools

```
recall(context_id=..., query="smoke test memory", k=5)
-> Verify: returns results array with length >= 1
-> Verify: at least one result matches the memory created in step 3

reference(memory_id=..., context_id=...)
-> Verify: returns full memory with summary, content, tags matching step 3

explore(memory_id=..., context_id=..., depth=2, min_weight=0.0)
-> Verify: returns response (total_activated >= 0, no error)
```

### 5. Memory update tools

```
update_memory(memory_id=..., context_id=..., summary="MCP smoke test memory — UPDATED", importance=0.7)
-> Verify: success response

recall(context_id=..., query="smoke test UPDATED", k=5)
-> Verify: returns updated memory with new summary
```

### 6. Edge CRUD tools

First, create a second test memory for edge testing (self-loops are not allowed):

```
remember(
  context_id=...,
  summary="MCP smoke test memory 2 — edge target",
  content="Second test memory for edge CRUD testing.",
  type="note",
  importance=0.5,
  tags=["smoke-test", "automated"]
)
-> Save returned memory_id as memory_id_2
```

```
create_edge(context_id=..., source_id=<memory_id>, target_id=<memory_id_2>, edge_type="related_to")
-> Verify: returns edge with weight=0.5, edge_type="related_to"

list_edges(context_id=..., memory_id=<memory_id>)
-> Verify: returns edges array with count >= 1

update_edge(context_id=..., source_id=<memory_id>, target_id=<memory_id_2>, weight=0.8)
-> Verify: returns updated edge with weight=0.8

delete_edge(context_id=..., source_id=<memory_id>, target_id=<memory_id_2>)
-> Verify: success response (edge deleted)
```

### 7. Merge & usage tools

Create a second temporary context, then test merge and usage:

```
create_context(name="smoke-test-merge-{unix_timestamp}", description="Merge target for smoke test.")
-> Save returned context_id as merge_target_id

merge_contexts(source_id=<context_id>, target_id=<merge_target_id>)
-> Verify: success response with merged memory count

get_usage()
-> Verify: returns plan, memories.used, contexts.used (no error)
```

Note: Sleep tools (`get_sleep_history`, `get_sleep_report`, `rollback_sleep_run`) are not tested here because they require a completed Sleep Maintenance run. Verify these manually after a sleep cycle.

### Cleanup

```
forget(memory_id=..., context_id=<merge_target_id>)
-> Verify: success response (merged memory deleted from target)

delete_context(context_id=<merge_target_id>)
-> Verify: success response (merge target deleted)

forget(memory_id=<memory_id_2>, context_id=...)
-> Verify: success response (memory 2 deleted from source)

delete_context(context_id=...)
-> Verify: success response (source context deleted)
```

### 8. Report

Print a summary table:

```
## MCP Smoke Test Results

| # | Tool | Action | Result |
|---|------|--------|--------|
| 1 | list_contexts | List contexts | PASS/FAIL |
| 2 | create_context | Create test context | PASS/FAIL |
| 3 | get_context_info | Get context details | PASS/FAIL |
| 4 | update_context | Update display name | PASS/FAIL |
| 5 | update_search_config | Update search weights | PASS/FAIL |
| 6 | remember | Create test memory | PASS/FAIL |
| 7 | recall | Search for memory | PASS/FAIL |
| 8 | reference | Get full memory | PASS/FAIL |
| 9 | explore | Graph traversal | PASS/FAIL |
| 10 | update_memory | Update memory | PASS/FAIL |
| 11 | recall (verify) | Verify update | PASS/FAIL |
| 12 | remember | Create 2nd test memory (edge target) | PASS/FAIL |
| 13 | create_edge | Create test edge | PASS/FAIL |
| 14 | list_edges | List edges | PASS/FAIL |
| 15 | update_edge | Update edge weight | PASS/FAIL |
| 16 | delete_edge | Delete edge | PASS/FAIL |
| 17 | create_context | Create merge target context | PASS/FAIL |
| 18 | merge_contexts | Merge source into target | PASS/FAIL |
| 19 | get_usage | Get workspace usage | PASS/FAIL |
| 20 | forget | Delete merged memory | PASS/FAIL |
| 21 | delete_context | Delete merge target | PASS/FAIL |
| 22 | forget | Delete memory 2 | PASS/FAIL |
| 23 | delete_context | Delete source context | PASS/FAIL |

**Result: N/23 passed**

Test context: smoke-test-{timestamp} (cleaned up)

Not tested (require prior Sleep Maintenance run):
- get_sleep_history, get_sleep_report, rollback_sleep_run
```

If any step fails:
- Mark it as FAIL with error message
- **Continue** with remaining steps where possible (skip dependent steps)
- Still attempt cleanup even if earlier steps failed
- Show total pass/fail count in summary
