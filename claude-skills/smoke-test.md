---
description: Run comprehensive smoke test of all MCP tools via live MCP connection
---

Verify MCP tools work correctly by executing them in sequence against temporary test contexts.
Exercises the core memory/edge/context/tag/analysis/sleep tools, the **agent-memory-substrate**
lane (`delivery_mode` pinning + `load_pinned`, the agent session-state lane, retrieval `feedback`,
and the `trust_tier` recall filter), and owner-scoped binding introspection.
When the caller is a workspace owner/admin, it also exercises the v0.49 Agent Control Plane
(registry, context bindings, and bootstrap composition).
Optionally exercises PRO-only resource rows (setup_resource, ingest_events, get_resource_impact, get_resource_schema, list_resource_tokens, plus delete_context cleanup) if the workspace has a PRO plan.

The canonical definitions are `backend/src/mcp_server/tools/_definitions.py` (**60 tools**). The
**Coverage cross-check** section near the end mirrors that registry so the "all MCP tools" claim
stays honest — every registered tool is either exercised here or listed there with a reason. This
is a live runbook, not a pytest suite, so the cross-check is a manual reconciliation step: when a
tool is added to the registry, this skill must gain a row or a documented exclusion.

Excluded by design (each documented in the Coverage cross-check):
- `analyze_context` — requires billing, BYOK, workspace owner role, and Pro-tier feature access.
- File tools (`init_file_upload`, `complete_file_upload`, `get_file_download_url`, `delete_file`, `list_files`) — require multipart S3/R2 upload flows that can't be exercised inline; cover them separately.
- Secret tools (`secret_register_pubkey`, `secret_put`, `secret_get`, `secret_list`, `secret_revoke_grant`) — zero-knowledge store needing `age` recipient keypairs, owner pubkey approval, and armored ciphertext; exercise via the `kagura secret` CLI/SDK, not inline.
- `setup_connector` — provisions an external connector (Slack/Discord/Teams) needing platform credentials and a real target; gated-skip inline.

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

remember(
  context_id=...,
  summary="MCP smoke test — time memory for recall_upcoming",
  content="Scheduled event seeded by smoke-test to verify recall_upcoming.",
  type="time",
  importance=0.5,
  tags=["smoke-test", "automated"],
  details={"trigger": {"year": 2099, "month": 1}}
)
-> Verify: returns a success response containing memory_id (UUID format)
-> Save returned time_memory_id

remember(
  context_id=...,
  summary="MCP smoke test — located memory for recall_nearby (WHERE axis)",
  content="Geo-tagged memory seeded by smoke-test to verify recall_nearby.",
  type="note",
  importance=0.5,
  tags=["smoke-test", "automated"],
  details={"location": {"lat": 35.6812, "lon": 139.7671, "label": "smoke-test anchor"}}
)
-> Verify: returns a success response containing memory_id (UUID format)
-> Save returned geo_memory_id

remember(
  context_id=...,
  summary="MCP smoke test — pinned memory for load_pinned (delivery_mode=always)",
  content="Goal/guardrail-style memory that must load deterministically every turn.",
  type="note",
  importance=0.6,
  tags=["smoke-test", "automated"],
  delivery_mode="always"
)
-> Verify: returns a success response containing memory_id (UUID format)
-> Verify: scope is "persistent" (delivery_mode="always" pins to persistent on write, no Sleep wait)
-> Save returned memory_id as pinned_memory_id
-> Note: this memory is unpinned then deleted in Cleanup so it does not leak into later runs
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

recall(context_id=..., query="smoke test memory", k=5, filters={"trust_tier": "trusted"})
-> Verify: status=success; results array returned
-> Verify: the memory from step 3 is present — manual writes are trusted-tier, and the filter
   excludes external/connector-ingested memories (this context has none, so trusted == all here)

reference(memory_id=..., context_id=...)
-> Verify: returns full memory with summary, content, tags matching step 3

explore(memory_id=..., context_id=..., depth=2, min_weight=0.0)
-> Verify: returns response (total_activated >= 0, no error)

recall_upcoming(context_id=..., from="now")
-> Verify: status=success
-> Verify: results array contains the time memory seeded in step 3 (time_memory_id present)
-> Verify: all returned results have type="time"

recall_nearby(context_id=..., lat=35.6812, lon=139.7671, radius_m=500)
-> Verify: status=success
-> Verify: results array contains the located memory seeded in step 3 (geo_memory_id present)
-> Verify: that result carries distance_m (a small number — the query point equals the stored point)

load_pinned(context_id=...)
-> Verify: status=success; returns the COMPLETE unranked set for delivery_mode="always"
   (deterministic counterpart to recall — no semantic search, no ranking, no rerank)
-> Verify: pinned_memory_id (from step 3) is present
-> Verify: truncated=false and total_available matches the returned count (small pinned set)

feedback(context_id=..., memory_id=<memory_id>, helpful=true, query="smoke test memory")
-> Verify: success response (append-only usefulness signal accepted)
-> Note: feedback is NOT embedded and is structurally excluded from recall(), so rating a result
   never pollutes the knowledge search space — there is nothing to assert in a later recall
```

### 4.5. Agent session-state lane (set_state / get_state)

TTL-bounded run-state, structurally excluded from recall(). Round-trip with a short TTL:

```
set_state(context_id=..., key="smoke-test-step", value={"phase": "running", "n": 1}, ttl_seconds=300)
-> Verify: success response (upsert accepted)

get_state(context_id=..., key="smoke-test-step")
-> Verify: returns value {"phase": "running", "n": 1} (round-trip intact)

set_state(context_id=..., key="smoke-test-step", value={"phase": "running", "n": 2}, ttl_seconds=300)
-> Verify: success (re-using the key overwrites the value)

get_state(context_id=...)
-> Verify: omitting key lists all live entries; "smoke-test-step" present with value n=2 (overwrite confirmed)
-> Verify: no expired entries are returned
-> Note: state is scoped to the context — it is removed when the context is deleted in Cleanup,
   and the 300s TTL expires it regardless; it never appears in recall()
```

### 5. Memory update tools

```
update_memory(memory_id=..., context_id=..., summary="MCP smoke test memory — UPDATED", importance=0.7)
-> Verify: success response

recall(context_id=..., query="smoke test UPDATED", k=5)
-> Verify: returns updated memory with new summary
```

### 6. Edge CRUD tools

First, create a second test memory for edge testing (self-loops are not allowed). Use `linked_memory_ids` to create a declared link at creation time (post-#741 this is stored as `origin="declared"`, **not** a `declared_link` edge_type — see the note on the verify step below):

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
-> Verify: list_edges(context_id=..., memory_id=<memory_id_2>) returns an outgoing edge whose
   target_id == <memory_id> (the linked memory), with weight=1.0 and confidence=1.0
-> Note (#741/#925): the linked_memory_ids declared link is stored as origin="declared" with
   edge_type="neural_association" — NOT a "declared_link" edge_type (that discriminator was removed
   in #741, which pivoted to the relation/origin two-axis model). MCP list_edges does not expose the
   origin axis, so the post-#741 verifiable signal is the edge to the linked target carrying the fixed
   declared weight/confidence of 1.0/1.0. A freshly created memory has had no recall co-activation, so
   this declared edge is the only edge present on memory_id_2 at this point. (The full declared-link
   reference surface — outgoing_links/incoming_links from #440 — is REST-only and not carried by the
   MCP reference() tool, so it cannot be asserted from this runbook.)
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
-> Verify: every returned tag, lowercased, starts with "smoke" (the prefix
   filter is case-insensitive per the MCP schema, so this stays correct even
   if a tag was stored as "Smoke-Foo")
```

### 6.6. Binding introspection (read-only)

Owner-scoped API-key binding introspection. No resource setup required — these are read-only:

```
list_my_bindings()
-> Verify: status=success; returns a bindings array (may be empty; count >= 0)
-> Save the first binding's id as binding_id, if any

describe_binding(binding_id=<binding_id from list_my_bindings>)
-> Verify: if list_my_bindings returned >= 1 binding, describe the first → success with its details
-> Verify: if no bindings exist, call describe_binding with a fake UUID
   ("00000000-0000-0000-0000-000000000000") and verify a not_found / permission_denied error
   instead (no side effects either way)
```

### 7. Merge & usage tools

Create a second temporary context, then test merge and usage:

```
create_context(name="smoke-test-merge-{unix_timestamp}", description="Merge target for smoke test.")
-> Save returned context_id as merge_target_id

merge_contexts(source_context_id=<context_id>, target_context_id=<merge_target_id>)
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

### 7.9. Agent Control Plane tools (owner/admin only)

**Pre-check:** These tools require the workspace owner/admin role. If the caller lacks that role,
skip this section and record "Agent Control Plane skipped — owner/admin required" in the report.
Type filters are intentionally omitted because they remain reserved until
[#1281](https://github.com/kagura-ai/memory-cloud/issues/1281).

```
register_agent(name="smoke-test-agent-{unix_timestamp}", description="Temporary MCP smoke-test agent", framework="codex", environment="test", version="smoke-1")
-> Verify: returns an active agent with enforcement_mode="enforce"
-> Save returned agent.id as agent_id

list_agents()
-> Verify: agents contains agent_id

get_agent(agent_id=<agent_id>)
-> Verify: returns the registered agent

bind_agent_context(agent_id=<agent_id>, context_id=..., can_read=true, write_policy="deny", is_default=true)
-> Verify: returns a default binding for the test context
-> Save returned binding.id as agent_binding_id

list_agent_bindings(agent_id=<agent_id>)
-> Verify: bindings contains agent_binding_id

update_agent_binding(agent_id=<agent_id>, binding_id=<agent_binding_id>, write_policy="direct")
-> Verify: changed includes write_policy and binding.write_policy="direct"

get_agent_bootstrap(agent_id=<agent_id>, query="MCP smoke test")
-> Verify: returns the default context and component results; degraded may be true only with an explained component error

update_agent(agent_id=<agent_id>, version="smoke-2")
-> Verify: changed includes version and agent.version="smoke-2"

unbind_agent_context(agent_id=<agent_id>, binding_id=<agent_binding_id>)
-> Verify: deleted=true

delete_agent(agent_id=<agent_id>)
-> Verify: deleted=true
```

If this section fails partway through, cleanup retries `unbind_agent_context` when a binding was
created and `delete_agent` when an agent was created. Treat an already-deleted/not-found result as
successful cleanup.

### 7.10. Connector tools (gated-skip)

`setup_connector` provisions an external connector (Slack/Discord/Teams) and requires platform
credentials plus a real connector target, so it is **not** exercised inline (it would create
external side effects):

```
setup_connector — SKIP (documented)
-> Reason: needs external connector platform credentials + a live target; cannot be exercised
   inline without side effects. Covered separately in connector integration tests.
```

### Cleanup

Remove any remaining Agent Control Plane artifacts, then unpin and delete the pinned memory (so
delivery_mode="always" state does not survive the run) and tear down the remaining artifacts. The
agent-state entry (set_state) needs no explicit delete — it is removed with its context below and
also expires via its TTL.

```
unbind_agent_context(agent_id=<agent_id>, binding_id=<agent_binding_id>)
-> Run only if Agent Control Plane cleanup did not already remove the binding

delete_agent(agent_id=<agent_id>)
-> Run only if Agent Control Plane cleanup did not already remove the agent

update_memory(memory_id=<pinned_memory_id>, context_id=..., delivery_mode="on_recall")
-> Verify: success response (pinned memory unpinned — no longer deterministically loaded)

forget(memory_id=<pinned_memory_id>, context_id=...)
-> Verify: success response (pinned memory deleted)

delete_context(context_id=<merge_target_id>)
-> Verify: success response (merge target soft-deleted, along with its memories)

forget(memory_id=<memory_id_2>, context_id=...)
-> Verify: success response (memory 2 deleted from source)

delete_context(context_id=...)
-> Verify: success response (source context deleted — also removes its agent-state entries)
```

### 8. Coverage cross-check (anti-drift)

Reconcile this skill against the canonical registry so the "all MCP tools" claim cannot silently
rot. The source of truth is `backend/src/mcp_server/tools/_definitions.py` (**60 tools**). Every
registered tool must be in exactly one column below. **If `_definitions.py` and this table
disagree, the skill is out of date — add a row (or a documented exclusion) before merging.**

Optional live assertion: count the named definitions and confirm it equals 60 (the number this
table is built for); if it differs, a tool was added/removed and this skill needs updating:

```
grep -cE '^\s*"name"\s*:\s*"[a-z_]+"' backend/src/mcp_server/tools/_definitions.py
-> Verify: equals 60 (else: reconcile this section with the definitions)
```

**Exercised inline (33 core tools):** list_contexts, create_context, get_context_info,
update_context, update_search_config, remember (incl. `delivery_mode="always"`), recall (incl.
`source_uri_prefix` / `source_type` / `trust_tier` filters), recall_upcoming, load_pinned,
feedback, set_state, get_state, reference, explore, update_memory, forget, list_edges, create_edge,
update_edge, delete_edge, list_tags, list_my_bindings, describe_binding, merge_contexts, get_usage,
list_analyses, get_active_analysis, get_analysis, get_cluster, get_sleep_history, get_sleep_report,
rollback_sleep_run, delete_context.

**Exercised with owner/admin role, else SKIP (10 tools):** register_agent, list_agents,
get_agent, update_agent, delete_agent, bind_agent_context, list_agent_bindings,
update_agent_binding, unbind_agent_context, get_agent_bootstrap.

**Exercised only on PRO plan, else SKIP (5 tools):** setup_resource, ingest_events,
get_resource_impact, get_resource_schema, list_resource_tokens.

**Documented exclusions / gated-skip (12 tools) — with reasons:**

| Tool | Why not exercised inline |
|---|---|
| `analyze_context` | Requires billing, BYOK key, workspace owner role, and Pro-tier feature access. |
| `setup_connector` | Provisions an external connector (Slack/Discord/Teams); needs platform credentials + a live target. Covered in connector integration tests. |
| `init_file_upload` | Multipart S3/R2 upload flow — can't be exercised inline. |
| `complete_file_upload` | Completes a multipart upload begun by `init_file_upload`. |
| `get_file_download_url` | Requires an uploaded object to produce a presigned URL. |
| `delete_file` | Requires an uploaded object to delete. |
| `list_files` | Grouped with the file-upload flow; covered in the file-tools test suite. |
| `secret_register_pubkey` | Zero-knowledge secret store: needs an `age` recipient public key. Covered by the secret-store suite / `kagura secret` CLI. |
| `secret_put` | Requires an owner-approved active recipient pubkey + armored `age` ciphertext encrypted client-side. |
| `secret_get` | Requires an active grant via an active recipient pubkey; the server never decrypts. |
| `secret_list` | Owner/admin metadata listing; grouped with the secret-store flow. |
| `secret_revoke_grant` | Operates on an existing grant produced by `secret_put`. |

33 + 10 + 5 + 12 = **60** — the full registry. The conditional rows are the 10 owner/admin Agent
Control Plane tools and 5 PRO-gated resource tools; the remaining 12 are documented exclusions.

### 9. Report

Print a summary table (numbers are illustrative; the executed order follows the sections above):

```
## MCP Smoke Test Results

| # | Tool | Action | Result |
|---|------|--------|--------|
| 1 | list_contexts | List contexts | PASS/FAIL |
| 2 | create_context | Create test context | PASS/FAIL |
| 3 | get_context_info | Get context details | PASS/FAIL |
| 4 | update_context | Update display name | PASS/FAIL |
| 5 | update_search_config | Update search weights | PASS/FAIL |
| 6 | remember | Create test memory (source_uri, source_type) | PASS/FAIL |
| 7 | remember | Create time memory | PASS/FAIL |
| 8 | remember | Create pinned memory (delivery_mode="always") | PASS/FAIL |
| 9 | recall | Search for memory | PASS/FAIL |
| 10 | recall | Search with include_explore_hints=true | PASS/FAIL |
| 11 | recall | Search with source_uri_prefix filter | PASS/FAIL |
| 12 | recall | Search with source_type filter | PASS/FAIL |
| 13 | recall | Search with trust_tier="trusted" filter | PASS/FAIL |
| 14 | reference | Get full memory | PASS/FAIL |
| 15 | explore | Graph traversal | PASS/FAIL |
| 16 | recall_upcoming | List upcoming time memories | PASS/FAIL |
| 17 | load_pinned | Deterministic load of pinned set | PASS/FAIL |
| 18 | feedback | Record helpful signal on a recall result | PASS/FAIL |
| 19 | set_state | Set + overwrite agent run-state (TTL) | PASS/FAIL |
| 20 | get_state | Read one key + list all live keys | PASS/FAIL |
| 21 | update_memory | Update memory | PASS/FAIL |
| 22 | recall (verify) | Verify update | PASS/FAIL |
| 23 | remember | Create 2nd memory (linked_memory_ids, linked_source_uris) | PASS/FAIL |
| 24 | list_edges (verify) | Verify declared link (linked_memory_ids → target, weight/confidence 1.0) | PASS/FAIL |
| 25 | create_edge | Create test edge | PASS/FAIL |
| 26 | list_edges | List edges | PASS/FAIL |
| 27 | update_edge | Update edge weight | PASS/FAIL |
| 28 | delete_edge | Delete edge | PASS/FAIL |
| 29 | list_tags | List tags in context | PASS/FAIL |
| 30 | list_tags | List tags with prefix filter | PASS/FAIL |
| 31 | list_my_bindings | List owner-scoped API-key bindings | PASS/FAIL |
| 32 | describe_binding | Describe a binding (or fake-id error) | PASS/FAIL |
| 33 | create_context | Create merge target context | PASS/FAIL |
| 34 | merge_contexts | Merge source into target | PASS/FAIL |
| 35 | get_usage | Get workspace usage | PASS/FAIL |
| 36 | list_analyses | List analysis runs | PASS/FAIL |
| 37 | get_active_analysis | Get latest succeeded analysis | PASS/FAIL |
| 38 | get_analysis | Get analysis by run_id (fake ID, error handling) | PASS/FAIL |
| 39 | get_cluster | Get cluster detail (fake run_id, error handling) | PASS/FAIL |
| 40 | get_sleep_history | Get sleep maintenance history | PASS/FAIL |
| 41 | get_sleep_report | Get sleep report (fake ID, error handling) | PASS/FAIL |
| 42 | rollback_sleep_run | Rollback sleep run (fake ID, error handling) | PASS/FAIL |
| 43 | update_memory | Unpin pinned memory (delivery_mode="on_recall") | PASS/FAIL |
| 44 | forget | Delete pinned memory | PASS/FAIL |
| 45 | delete_context | Soft-delete merge target and its memories | PASS/FAIL |
| 46 | forget | Delete memory 2 | PASS/FAIL |
| 47 | delete_context | Delete source context (+ its agent-state) | PASS/FAIL |
| A1 | register_agent | Register temporary agent (owner/admin only) | PASS/FAIL/SKIP |
| A2 | list_agents | List registry and find temporary agent (owner/admin only) | PASS/FAIL/SKIP |
| A3 | get_agent | Get temporary agent (owner/admin only) | PASS/FAIL/SKIP |
| A4 | bind_agent_context | Bind test context as default (owner/admin only) | PASS/FAIL/SKIP |
| A5 | list_agent_bindings | List bindings (owner/admin only) | PASS/FAIL/SKIP |
| A6 | update_agent_binding | Set write_policy=direct (owner/admin only) | PASS/FAIL/SKIP |
| A7 | get_agent_bootstrap | Compose default-context bootstrap (owner/admin only) | PASS/FAIL/SKIP |
| A8 | update_agent | Update agent version (owner/admin only) | PASS/FAIL/SKIP |
| A9 | unbind_agent_context | Remove temporary binding (owner/admin only) | PASS/FAIL/SKIP |
| A10 | delete_agent | Delete temporary agent (owner/admin only) | PASS/FAIL/SKIP |
| P1 | setup_resource | Create resource context + token (PRO only) | PASS/FAIL/SKIP |
| P2 | ingest_events | Batch ingest 2 test events (PRO only) | PASS/FAIL/SKIP |
| P3 | get_resource_impact | Get resource stats (PRO only) | PASS/FAIL/SKIP |
| P4 | get_resource_schema | Get schema (expect not_found) (PRO only) | PASS/FAIL/SKIP |
| P5 | list_resource_tokens | List tokens for resource (PRO only) | PASS/FAIL/SKIP |
| P6 | delete_context | Delete resource context (PRO only) | PASS/FAIL/SKIP |

**Result: N/47 core rows passed** (+ N/10 Agent Control Plane rows and N/6 PRO resource rows
passed, or SKIP when the corresponding gate is unavailable)

Note: the 47 rows are test *steps*, not distinct tools — several tools (remember, recall,
update_memory, forget, delete_context, list_edges, list_tags) are exercised in multiple rows.
Distinct-tool coverage is reconciled in the Coverage cross-check
(33 core + 10 owner/admin + 5 PRO + 12 documented-skip = 60).

Test context: smoke-test-{timestamp} (cleaned up)

Documented exclusions / gated-skip (see Coverage cross-check) — not counted as FAIL:
- analyze_context (billing + BYOK + owner + Pro-tier)
- Agent Control Plane tools may be role-gated (owner/admin); if skipped, list A1–A10 as SKIP
- setup_connector (external connector credentials + live target)
- File tools: init_file_upload, complete_file_upload, get_file_download_url, delete_file, list_files (multipart S3/R2)
- Secret tools: secret_register_pubkey, secret_put, secret_get, secret_list, secret_revoke_grant (zero-knowledge: age keypairs + owner approval + ciphertext)

Registry reconciliation: 33 core + 10 owner/admin + 5 PRO + 12 documented-skip = 60 tools in _definitions.py.
```

If any step fails:
- Mark it as FAIL with error message
- **Continue** with remaining steps where possible (skip dependent steps)
- Still attempt cleanup even if earlier steps failed (including unpinning the pinned memory)
- Show total pass/fail count in summary
