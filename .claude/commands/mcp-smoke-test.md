---
description: Run comprehensive smoke test of all MCP tools via live MCP connection
---

Verify every MCP tool works correctly by executing them in sequence against a temporary test context.
Use this after deployments, tool description changes, or MCP server updates.

**Prerequisite:** MCP server must be running and connected (e.g. via `/docker status`).

## Steps

### 1. Preparation

Create a temporary test context for isolation:

```
list_contexts()
→ Verify: returns a list with count >= 0

create_context(name="smoke-test-{unix_timestamp}", description="Temporary context for MCP smoke test. Safe to delete.")
→ Verify: returns context with id (UUID format)
→ Save returned context_id for all subsequent steps
```

### 2. Context tools

```
get_context_info(context_id=..., include_details=true)
→ Verify: returns context.name matching "smoke-test-*", context.id matches

update_context(context_id=..., display_name="Smoke Test", summary="Temporary smoke test context")
→ Verify: success response

update_search_config(context_id=..., semantic_weight=0.6, bm25_weight=0.4)
→ Verify: success response
```

### 3. Memory write tools

```
remember(
  context_id=...,
  summary="MCP smoke test memory — testing remember tool",
  content="This is a test memory created by /mcp-smoke-test. If you see this, the remember tool is working correctly.",
  type="note",
  importance=0.5,
  tags=["smoke-test", "automated"],
  context_summary="Created during automated MCP smoke test for verification purposes."
)
→ Verify: returns memory with id (UUID format)
→ Save returned memory_id
```

### 4. Memory read tools

```
recall(context_id=..., query="smoke test memory", k=5)
→ Verify: returns results array with length >= 1
→ Verify: at least one result matches the memory created in step 3

reference(memory_id=..., context_id=...)
→ Verify: returns full memory with summary, content, tags matching step 3

explore(memory_id=..., context_id=..., depth=2, min_weight=0.0)
→ Verify: returns response (total_activated >= 0, no error)
```

### 5. Memory update tools

```
update_memory(memory_id=..., context_id=..., summary="MCP smoke test memory — UPDATED", importance=0.7)
→ Verify: success response

recall(context_id=..., query="smoke test UPDATED", k=5)
→ Verify: returns updated memory with new summary
```

### 6. Cleanup

```
forget(memory_id=..., context_id=...)
→ Verify: success response (memory deleted)

delete_context(context_id=...)
→ Verify: success response (context deleted)
```

### 7. Static tools

```
kagura_memory_usage_guide()
→ Verify: returns non-empty guide text
```

### 8. Report

Print a summary table:

```
## MCP Smoke Test Results

| # | Tool | Action | Result |
|---|------|--------|--------|
| 1 | list_contexts | List contexts | ✅ PASS |
| 2 | create_context | Create test context | ✅ PASS |
| 3 | get_context_info | Get context details | ✅ PASS |
| 4 | update_context | Update display name | ✅ PASS |
| 5 | update_search_config | Update search weights | ✅ PASS |
| 6 | remember | Create test memory | ✅ PASS |
| 7 | recall | Search for memory | ✅ PASS |
| 8 | reference | Get full memory | ✅ PASS |
| 9 | explore | Graph traversal | ✅ PASS |
| 10 | update_memory | Update memory | ✅ PASS |
| 11 | recall (verify) | Verify update | ✅ PASS |
| 12 | forget | Delete memory | ✅ PASS |
| 13 | delete_context | Delete test context | ✅ PASS |
| 14 | kagura_memory_usage_guide | Get usage guide | ✅ PASS |

**Result: 14/14 passed** ✅

Test context: smoke-test-{timestamp} (cleaned up)
```

If any step fails:
- Mark it as ❌ FAIL with error message
- **Continue** with remaining steps where possible (skip dependent steps)
- Still attempt cleanup (steps 6) even if earlier steps failed
- Show total pass/fail count in summary
