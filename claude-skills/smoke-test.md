---
description: Run comprehensive smoke test of all MCP tools via live MCP connection
---

Verify every MCP tool works correctly by executing them in sequence against a temporary test context.
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

```
create_edge(context_id=..., source_id=<memory_id>, target_id=<memory_id>, edge_type="related_to")
-> Verify: returns edge with weight=0.5, edge_type="related_to"
-> Save source_id and target_id for subsequent steps

list_edges(context_id=..., memory_id=<memory_id>)
-> Verify: returns edges array with count >= 1

update_edge(context_id=..., source_id=<source_id>, target_id=<target_id>, weight=0.8)
-> Verify: returns updated edge with weight=0.8

delete_edge(context_id=..., source_id=<source_id>, target_id=<target_id>)
-> Verify: success response (edge deleted)
```

### 7. Cleanup

```
forget(memory_id=..., context_id=...)
-> Verify: success response (memory deleted)

delete_context(context_id=...)
-> Verify: success response (context deleted)
```

### 7. Report

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
| 12 | create_edge | Create test edge | PASS/FAIL |
| 13 | list_edges | List edges | PASS/FAIL |
| 14 | update_edge | Update edge weight | PASS/FAIL |
| 15 | delete_edge | Delete edge | PASS/FAIL |
| 16 | forget | Delete memory | PASS/FAIL |
| 17 | delete_context | Delete test context | PASS/FAIL |

**Result: N/17 passed**

Test context: smoke-test-{timestamp} (cleaned up)
```

If any step fails:
- Mark it as FAIL with error message
- **Continue** with remaining steps where possible (skip dependent steps)
- Still attempt cleanup (step 6) even if earlier steps failed
- Show total pass/fail count in summary
