---
description: Run comprehensive smoke test of all MCP tools via live MCP connection
---

Verify MCP tools work correctly by executing them in sequence against temporary test contexts.
Tests 18 of 21 memory/context tools (excludes 3 sleep tools that require a prior Sleep Maintenance run).
Optionally tests 5 resource tools if the workspace has a PRO plan.
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
  context_summary="Created during automated MCP smoke test for verification purposes.",
  source_uri="file:///smoke-test/test-memory.md",
  source_type="file"
)
-> Verify: returns a success response containing memory_id (UUID format)
-> Save returned memory_id
-> Note: source_uri/source_type are persisted but not in the remember response; validated via recall filters in step 4
```

### 4. Memory read tools

```
recall(context_id=..., query="smoke test memory", k=5)
-> Verify: returns results array with length >= 1
-> Verify: at least one result matches the memory created in step 3

recall(context_id=..., query="smoke test memory", k=5, include_explore_hints=true)
-> Verify: response contains explore_hints field (array)
-> Verify: if explore_hints is non-empty, at least one hint has reason "top_result"
-> Verify: empty explore_hints is acceptable (best-effort generation) and should not fail the smoke test

recall(context_id=..., query="smoke test memory", k=5, filters={"source_uri_prefix": "file:///smoke-test/"})
-> Verify: results contain the memory_id from step 3 (confirms source_uri filter works)

recall(context_id=..., query="smoke test memory", k=5, filters={"source_type": "file"})
-> Verify: results contain the memory_id from step 3 (confirms source_type filter works)

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

First, create a second test memory for edge testing (self-loops are not allowed). Use `linked_memory_ids` to create a `declared_link` edge at creation time:

```
remember(
  context_id=...,
  summary="MCP smoke test memory 2 — edge target",
  content="Second test memory for edge CRUD testing.",
  type="note",
  importance=0.5,
  tags=["smoke-test", "automated"],
  linked_memory_ids=[<memory_id>],
  linked_source_uris=["file:///smoke-test/test-memory.md"]
)
-> Save returned memory_id as memory_id_2
-> Verify: list_edges(context_id=..., memory_id=<memory_id_2>) returns at least one edge with edge_type="declared_link"
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

### 7.5. Resource tools (PRO plan only)

**Pre-check:** Call `get_usage()` and check the plan. If the plan is `free` or `basic`, skip this section entirely and note "Resource tools skipped — PRO plan required" in the report.

```
setup_resource(name="smoke-test-resource-{unix_timestamp}", resource_id="smoke_test_{unix_timestamp}")
-> Verify: returns context_id (UUID), resource_id, token (plaintext), token_id
-> Save context_id as resource_context_id, resource_id, and token

ingest_events(resource_id=<resource_id>, events=[
  {"op": "upsert", "doc_id": "TEST-001", "version": 1, "payload": {"name": "Test Product", "price": 1000}},
  {"op": "upsert", "doc_id": "TEST-002", "version": 1, "payload": {"name": "Test Product 2", "price": 2000}}
])
-> Verify: created_count=2, failed_count=0, event_ids has 2 entries

get_resource_impact(resource_id=<resource_id>)
-> Verify: token_count >= 1, current_schema_version is null (no schema created)

get_resource_schema(resource_id=<resource_id>)
-> Verify: returns schema_not_found error (expected — no schema exists yet)

list_resource_tokens(resource_id=<resource_id>)
-> Verify: returns tokens array with at least 1 token matching resource_id
```

**Resource cleanup** (runs even if some steps failed):

```
delete_context(context_id=<resource_context_id>)
-> Verify: success response (resource context soft-deleted)
```

### Cleanup

```
delete_context(context_id=<merge_target_id>)
-> Verify: success response (merge target soft-deleted, along with its memories)

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
| 6 | remember | Create test memory (with source_uri, source_type) | PASS/FAIL |
| 7 | recall | Search for memory | PASS/FAIL |
| 8 | recall | Search with include_explore_hints=true | PASS/FAIL |
| 9 | recall | Search with source_uri_prefix filter | PASS/FAIL |
| 10 | recall | Search with source_type filter | PASS/FAIL |
| 11 | reference | Get full memory | PASS/FAIL |
| 12 | explore | Graph traversal | PASS/FAIL |
| 13 | update_memory | Update memory | PASS/FAIL |
| 14 | recall (verify) | Verify update | PASS/FAIL |
| 15 | remember | Create 2nd memory (with linked_memory_ids, linked_source_uris) | PASS/FAIL |
| 16 | list_edges (verify) | Verify declared_link edge created | PASS/FAIL |
| 17 | create_edge | Create test edge | PASS/FAIL |
| 18 | list_edges | List edges | PASS/FAIL |
| 19 | update_edge | Update edge weight | PASS/FAIL |
| 20 | delete_edge | Delete edge | PASS/FAIL |
| 21 | create_context | Create merge target context | PASS/FAIL |
| 22 | merge_contexts | Merge source into target | PASS/FAIL |
| 23 | get_usage | Get workspace usage | PASS/FAIL |
| 24 | delete_context | Soft-delete merge target and its memories | PASS/FAIL |
| 25 | forget | Delete memory 2 | PASS/FAIL |
| 26 | delete_context | Delete source context | PASS/FAIL |
| 27 | setup_resource | Create resource context + token (PRO only) | PASS/FAIL/SKIP |
| 28 | ingest_events | Batch ingest 2 test events (PRO only) | PASS/FAIL/SKIP |
| 29 | get_resource_impact | Get resource stats (PRO only) | PASS/FAIL/SKIP |
| 30 | get_resource_schema | Get schema (expect not_found) (PRO only) | PASS/FAIL/SKIP |
| 31 | list_resource_tokens | List tokens for resource (PRO only) | PASS/FAIL/SKIP |
| 32 | delete_context | Delete resource context (PRO only) | PASS/FAIL/SKIP |

**Result: N/26 passed** (+ N/6 resource tools passed, or skipped if not PRO)

Test context: smoke-test-{timestamp} (cleaned up)

Not tested (require prior Sleep Maintenance run):
- get_sleep_history, get_sleep_report, rollback_sleep_run

Resource tools (PRO plan only):
- If plan is free/basic: all 6 resource rows show SKIP
- setup_resource, ingest_events, get_resource_impact, get_resource_schema, list_resource_tokens
```

If any step fails:
- Mark it as FAIL with error message
- **Continue** with remaining steps where possible (skip dependent steps)
- Still attempt cleanup even if earlier steps failed
- Show total pass/fail count in summary
