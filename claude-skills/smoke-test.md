---
description: Run comprehensive smoke test of all MCP tools via live MCP connection
---

Verify MCP tools work correctly by executing them in sequence against temporary test contexts.
Exercises 35 non-resource test rows covering the core memory/edge/context/tag/analysis/sleep tools.
Optionally exercises 6 PRO-only rows for resource tools (setup_resource, ingest_events, get_resource_impact, get_resource_schema, list_resource_tokens, plus delete_context cleanup) if the workspace has a PRO plan.

Excluded by design:
- `analyze_context` — requires billing, BYOK, workspace owner role, and Pro-tier feature access.
- File-upload tools (`init_file_upload`, `complete_file_upload`, `get_file_download_url`, `delete_file`, `list_files`) — require multipart S3/R2 upload flows that can't be exercised inline; cover them separately.

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

### 6.5. Tag discovery

```
list_tags(context_id=...)
-> Verify: returns {status: "success", tags: [...], total: N} with N >= 1
-> Verify: at least one entry has tag="smoke-test" (created via remember in step 3)

list_tags(context_id=..., prefix="smoke")
-> Verify: every returned tag starts with "smoke"
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

### 7.6. Analysis tools

Note: `analyze_context` is **not** included because it requires workspace owner role, Pro-tier feature access, billing, a configured BYOK key, and per-day quota availability.

**Pre-condition:** `list_analyses` and `get_active_analysis` require the workspace owner role and the analysis feature to be available. Valid responses include gate errors (`permission_denied`, `feature_not_available`) — the smoke test should treat these as acceptable outcomes, not failures.

```
list_analyses(context_id=...)
-> Verify: returns items array, or gate error (`permission_denied`, `feature_not_available`) — treat all as PASS

get_active_analysis(context_id=...)
-> Verify: returns analysis run object, `no_succeeded_run`, or gate error (`permission_denied`, `feature_not_available`) — treat all as PASS

get_analysis(run_id="00000000-0000-0000-0000-000000000000")
-> Verify: returns `run_not_found` error (expected — fake run_id)

get_analysis(run_id="this-is-not-a-uuid")
-> Verify: returns error response (invalid UUID format)
```

```
get_cluster(run_id="00000000-0000-0000-0000-000000000000", cluster_index=0)
-> Verify: returns `cluster_not_found` error (expected — fake run_id)

get_cluster(run_id="this-is-not-a-uuid", cluster_index=0)
-> Verify: returns error response (invalid UUID format)
```

### 7.7. Sleep tools

Note: `get_sleep_history` and `get_sleep_report` are read-only inspection tools. `rollback_sleep_run` is mutating — fake IDs verify error handling without side effects.

```
get_sleep_history(context_id=...)
-> Verify: returns `{reports: [...], count: ...}` (no error; may be empty)

get_sleep_history(context_id=..., limit=3)
-> Verify: returns at most 3 reports in the `reports` array (no error)
```

```
get_sleep_report(report_id="00000000-0000-0000-0000-000000000000")
-> Verify: returns `report_not_found` error (expected — fake report_id)

get_sleep_report(report_id="this-is-not-a-uuid")
-> Verify: returns `invalid_report_id` error (invalid UUID format)
```

```
rollback_sleep_run(report_id="00000000-0000-0000-0000-000000000000")
-> Verify: returns `report_not_found` error (expected — fake report_id)

rollback_sleep_run(report_id="this-is-not-a-uuid")
-> Verify: returns `invalid_report_id` error (invalid UUID format)
```

### 7.8. Resource tools (PRO plan only)

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
| 21 | list_tags | List tags in context | PASS/FAIL |
| 22 | list_tags | List tags with prefix filter | PASS/FAIL |
| 23 | create_context | Create merge target context | PASS/FAIL |
| 24 | merge_contexts | Merge source into target | PASS/FAIL |
| 25 | get_usage | Get workspace usage | PASS/FAIL |
| 26 | delete_context | Soft-delete merge target and its memories | PASS/FAIL |
| 27 | forget | Delete memory 2 | PASS/FAIL |
| 28 | delete_context | Delete source context | PASS/FAIL |
| 29 | setup_resource | Create resource context + token (PRO only) | PASS/FAIL/SKIP |
| 30 | ingest_events | Batch ingest 2 test events (PRO only) | PASS/FAIL/SKIP |
| 31 | get_resource_impact | Get resource stats (PRO only) | PASS/FAIL/SKIP |
| 32 | get_resource_schema | Get schema (expect not_found) (PRO only) | PASS/FAIL/SKIP |
| 33 | list_resource_tokens | List tokens for resource (PRO only) | PASS/FAIL/SKIP |
| 34 | delete_context | Delete resource context (PRO only) | PASS/FAIL/SKIP |
| 35 | list_analyses | List analysis runs | PASS/FAIL |
| 36 | get_active_analysis | Get latest succeeded analysis | PASS/FAIL |
| 37 | get_analysis | Get analysis by run_id (fake ID, error handling) | PASS/FAIL |
| 38 | get_cluster | Get cluster detail (fake run_id, error handling) | PASS/FAIL |
| 39 | get_sleep_history | Get sleep maintenance history | PASS/FAIL |
| 40 | get_sleep_report | Get sleep report (fake ID, error handling) | PASS/FAIL |
| 41 | rollback_sleep_run | Rollback sleep run (fake ID, error handling) | PASS/FAIL |

**Result: N/35 passed** (+ N/6 resource tools passed, or skipped if not PRO)

Test context: smoke-test-{timestamp} (cleaned up)

Not tested (requires billing + BYOK):
- analyze_context

Resource tools (PRO plan only):
- If plan is free/basic: all 6 resource rows show SKIP
- setup_resource, ingest_events, get_resource_impact, get_resource_schema, list_resource_tokens
```

If any step fails:
- Mark it as FAIL with error message
- **Continue** with remaining steps where possible (skip dependent steps)
- Still attempt cleanup even if earlier steps failed
- Show total pass/fail count in summary
