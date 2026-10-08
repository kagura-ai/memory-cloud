---
description: Run comprehensive smoke test of all MCP tools via live MCP connection (needs the full tool profile and a workspace owner)
---

Verify MCP tools work correctly by executing them in sequence against temporary test contexts.
Exercises the memory/edge/context/tag/analysis/sleep tools (incl. the WHEN/WHERE axes via
`recall_upcoming` / `recall_nearby`), the tool-surface tools added in v0.93.0 (`describe_tools`,
`guide`, `remember_batch`, `list`, `changes_since`, `bootstrap`), the **agent-memory-substrate** lane (`delivery_mode` pinning +
`load_pinned`, `load_guardrails`, the agent session-state lane, the measurement lane (`record_measurement` +
`recall_series`), retrieval `feedback`, and the `trust_tier` recall filter), and owner-scoped
binding introspection.
When the caller is a workspace owner/admin, it also exercises the v0.49 Agent Control Plane
(registry, context bindings, and bootstrap composition).
Optionally exercises the XL-only resource rows (setup_resource, ingest_events, get_resource_impact, get_resource_schema, list_resource_tokens, plus delete_context cleanup) if the workspace plan has the `resources` feature (XL / `promax`; since #1551 lower tiers may only keep serving resources that already exist).

The canonical definitions are `backend/src/mcp_server/tools/_definitions.py` (**70 tools**). The
**Coverage cross-check** section near the end mirrors that registry so the "all MCP tools" claim
stays honest — every registered tool is either exercised here or listed there with a reason. This
is a live runbook, not a pytest suite, so the run itself checks the count through `describe_tools`
(step 0), and `backend/tests/mcp_server/test_smoke_test_runbook.py` fails when a registered tool
is missing from the cross-check or a stated total differs from the registry: when a tool is added,
this skill must gain a row or a documented exclusion.

Excluded by design (each documented in the Coverage cross-check):
- `analyze_context` — requires billing, BYOK, workspace owner role, and Pro-tier feature access.
- File tools (`init_file_upload`, `complete_file_upload`, `get_file_download_url`, `delete_file`, `list_files`) — require multipart S3/R2 upload flows that can't be exercised inline; cover them separately.
- Secret tools (`secret_register_pubkey`, `secret_put`, `secret_get`, `secret_list`, `secret_revoke_grant`) — zero-knowledge store needing `age` recipient keypairs, owner pubkey approval, and armored ciphertext; exercise via the `kagura secret` CLI/SDK, not inline.
- `setup_connector` — provisions an external connector (Slack/Discord/Teams) needing platform credentials and a real target; gated-skip inline.

Use this after deployments, tool description changes, or MCP server updates.

**Prerequisites:**

- The MCP server is running and connected.
- **The connection lists every tool.** `tools/list` defaults to the 18-tool core profile, and most
  clients let the model call listed tools only — `create_context` in step 1 is already outside it.
  Connect with the full list: put `?profile=full` on the endpoint URL
  (`https://<your-domain>/mcp/w/<workspace-id>?profile=full`, or `&profile=full` when the URL
  already has a query), or pass `--tool-profile full` to the `kagura-mcp` stdio bridge. A profile
  only changes what `tools/list` shows, never what a credential may do. Step 0 checks this and
  stops the run on a narrower connection.
- **The caller is a workspace owner.** Creating the private test contexts needs the workspace
  owner or admin role (`create_context` answers `permission_denied` with
  `required_role="owner or admin"` otherwise). An admin can run the main rows; the analysis rows
  (7.6) then answer their owner gate, which counts as PASS, and the resource-token revoke in 7.8
  is owner-only.
- The MCP URL has no `?guardrails=off` if you want the `guardrails` block of `bootstrap` (6.7)
  checked; with it the block is absent and that one check is skipped.

## Steps

### 0. Tool list and manual (read-only — run before anything is created)

```
describe_tools()
-> Verify: status=success; `listed` (the names this URL's tools/list returns, sorted) has 70
   entries and `count` (the tools the URL leaves out, itemised in `tools`) is 0
-> Verify: len(listed) + count == 70 on any connection — the registry total this runbook is built
   for. A different sum means a tool was added or removed: reconcile the Coverage cross-check
   (section 8) before trusting the run
-> ABORT when count > 0 (a default connection answers count=52 with the 18 core tools in
   `listed`): create nothing, report step 0 as FAIL and tell the user to reconnect with the full
   tool list — `?profile=full` on the MCP endpoint URL, or `--tool-profile full` for the
   `kagura-mcp` stdio bridge — then run the smoke test again. The reply's `url` block says the same
-> Verify: no `url_error` key (it carries the message tools/list fails with when the URL's
   `profile` / `tools` selection is broken)

describe_tools(names=["create_context", "no_such_tool"])
-> Verify: `definitions` has exactly one entry, name="create_context", with its `inputSchema`;
   unknown == ["no_such_tool"]

guide(topics=["index"])
-> Verify: status=success; topics == [{topic: "index", text: <the topic list>}] and the text names
   `remember_batch`, `list`, `changes_since` and `bootstrap`

guide(topics=["remember_batch", "no-such-topic"])
-> Verify: a bare tool name expands to its sections — topics contains `remember_batch.usage`, each
   entry with a non-empty `text`; unknown == ["no-such-topic"] and a `hint` points at the index
```

### 1. Preparation

Create a temporary test context for isolation:

```
list_contexts()
-> Verify: status=success; `contexts` is an array and `total` equals its length (the number of
   items on this page). `count` is NOT the list length — it is the workspace's context quota usage,
   next to `limit` and `can_create`; `has_more` / `next_cursor` page the list
-> Verify: items carry only id, name, is_private, is_locked, last_used_at — no `summary` /
   `embedding_model` (opt-in via include_summary / include_details), so the response stays small
   even on a workspace with many contexts
-> Note: this run creates two contexts (three with the resource rows). If `can_create` is false
   the context quota is exhausted: stop here and report it instead of failing step 1 halfway

create_context(name="smoke-test-{unix_timestamp}", description="Temporary context for MCP smoke test. Safe to delete.")
-> Verify: status=success with a top-level `context_id` (UUID format) — there is no nested context
   object and no `id` key; context_name echoes the name and context_is_private is true (the default)
-> Save returned context_id for all subsequent steps

list_contexts(name_contains="smoke-test-{unix_timestamp}", include_summary=true)
-> Verify: total == 1 and contexts[0].id is the context created above; the item has a `summary` key
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
-> Verify (compact acknowledgement): the `persistence` block carries scope, committed=true,
   promotes_via and consolidation_archive_min_age_days but NO `detail` key — the prose is opt-in
   with verbose=true and lives in guide(["persistence"])
-> Note: the block is advisory — the server omits it when it cannot classify the scope. A reply
   without `persistence` is not a FAIL for the write; say so in the report and skip the
   persistence checks of that row
-> Save returned memory_id
-> Note: source_uri/source_type are persisted but not in the remember response; validated via recall filters in step 4

remember(
  context_id=...,
  summary="MCP smoke test — time memory for recall_upcoming",
  content="Scheduled event seeded by smoke-test to verify recall_upcoming.",
  type="time",
  importance=0.5,
  tags=["smoke-test", "automated"],
  details={"trigger": {"year": 2099, "month": 1}},
  verbose=true
)
-> Verify: returns a success response containing memory_id (UUID format)
-> Verify (verbose acknowledgement): persistence.detail is a non-empty string — the same block as
   above plus the lifecycle prose
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

remember(
  context_id=...,
  summary="MCP smoke test — guardrail: remove the worktree before gh pr merge --delete-branch",
  content="Tool-guardrail memory: load_guardrails must serve it with the normalized tool_trigger.",
  type="troubleshooting",
  importance=0.7,
  tags=["smoke-test", "automated"],
  details={"tool_trigger": {"tool": "Bash|PowerShell", "match": "gh pr merge\\b.*--delete-branch"}}
)
-> Verify: returns a success response containing memory_id (UUID format)
-> Save returned memory_id as guardrail_memory_id
-> Note: marking a guardrail needs context editor or above (a workspace owner/admin passes);
   a lower role gets permission_denied with required_role="editor" — report that as SKIP, not FAIL

remember(
  context_id=...,
  summary="MCP smoke test — a guardrail with a nested quantifier must be rejected",
  content="Negative test: the safe-regex grammar rejects (a+)+.",
  type="note",
  details={"tool_trigger": {"tool": "Bash", "match": "(a+)+"}}
)
-> Verify: status="error", error="validation_error", message contains "regex_nested_quantifier"
-> Verify: no memory_id is returned (nothing to clean up)
```

### 4. Memory read tools

```
recall(context_id=..., query="smoke test memory", k=5)
-> Verify: returns results array with length >= 1
-> Verify: at least one result matches the memory created in step 3
-> Verify: that result has a `tags` array holding "smoke-test" and "automated" (tags are present
   when non-empty) and updated_at == null — a memory that was never edited has no updated_at; created_at is set
-> Verify: a top-level `related_tags`, when present, is a non-empty array — the key is omitted
   rather than sent as []

list_tags(context_id=..., prefx="smoke")
-> Verify: status="error", error="invalid_argument" — `prefx` is not declared by the schema, so the
   dispatcher refuses the call instead of silently dropping the argument; unknown_arguments ==
   ["prefx"], suggestions == {"prefx": "prefix"}, and allowed_arguments lists the declared names
-> Note: a client that validates arguments against the inputSchema itself may refuse to send this
   call. Record SKIP ("client-side validation") in that case, not FAIL

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
-> Verify: that result carries `location` exactly as stored in details.location —
   {lat: 35.6812, lon: 139.7671, label: "smoke-test anchor"} — and no `details` key; the full
   details is opt-in with include_details=true

load_pinned(context_id=...)
-> Verify: status=success; returns the COMPLETE unranked set for delivery_mode="always"
   (deterministic counterpart to recall — no semantic search, no ranking, no rerank)
-> Verify: pinned_memory_id (from step 3) is present
-> Verify: truncated=false and total_available matches the returned count (small pinned set)

load_guardrails(context_id=...)
-> Verify: status=success; format=1; version is a 16-character hex string
-> Verify: tool_triggered contains guardrail_memory_id, and its tool_trigger is
   {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge\\b.*--delete-branch", "action": "inform"}
   (on / action defaults filled in by the server; key order tool, on, match, action)
-> Verify: pinned contains pinned_memory_id (the trusted-tier pinned lane); items carry source_type
   and authored_by_caller=true; no item carries content
-> Verify: truncated=false; total_available = pinned_total_available + tool_triggered_total_available

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
-> Verify: returns key="smoke-test-step", found=true and value {"phase": "running", "n": 1}
   (round-trip intact)

set_state(context_id=..., key="smoke-test-step", value={"phase": "running", "n": 2}, ttl_seconds=300)
-> Verify: success (re-using the key overwrites the value)

get_state(context_id=...)
-> Verify: omitting key lists all live entries as {status, states: {key: value, ...}, count,
   has_more, next_cursor}; "smoke-test-step" present with value n=2 (overwrite confirmed) and
   count >= 1; has_more=false and next_cursor=null on this one-entry page
-> Verify: no expired entries are returned
-> Note: state is scoped to the context — it is removed when the context is deleted in Cleanup,
   and the 300s TTL expires it regardless; it never appears in recall()
```

### 4.6. Measurement lane (record_measurement / recall_series)

The HOW-MUCH lane (#1333): an append-only numeric series, structurally excluded from recall()
(measurements are not embedded, and Sleep never merges or rewrites them). Record two observations —
one "now" and one backdated 48 hours via `measured_at` — then read them back bucketed two ways:

```
record_measurement(context_id=..., metric="smoke_test_metric", value=10, unit="units", details={"source": "smoke-test"})
-> Verify: status=success; returns measurement_id (UUID), metric="smoke_test_metric", value=10.0,
   unit="units", and measured_at as a UTC ISO timestamp ending in "Z" (defaults to now)
-> Save returned measured_at as measured_at_now

record_measurement(context_id=..., metric="smoke_test_metric", value=30, unit="units", measured_at="{measured_at_now - 48h, ISO 8601}")
-> Verify: status=success; measured_at echoes the backdated timestamp, normalized to UTC ("Z" suffix)
-> Note: each call appends a row — nothing is upserted — so the counts below assume exactly these
   two rows in a fresh context; re-running the lane in the same context doubles them

recall_series(context_id=..., metric="smoke_test_metric")
-> Verify: status=success, period="day", agg="avg" (the defaults), count=2
-> Verify: series has one bucket per UTC day, ascending — the backdated day first:
   {bucket: "<UTC day of measured_at - 48h>T00:00:00Z", value: 30.0, count: 1}, then
   {bucket: "<UTC day of measured_at_now>T00:00:00Z", value: 10.0, count: 1}
-> Verify: no bucket for the day in between (empty buckets are omitted, not zero-filled)
-> Note: buckets align to UTC boundaries, so the two observations always land in two different
   day buckets even when a local-time day would put them in one

recall_series(context_id=..., metric="smoke_test_metric", period="month", agg="sum")
-> Verify: status=success, period="month", agg="sum"
-> Verify: on every day but the 1st–2nd of a UTC month both observations share one month bucket, so
   the normal result is count=1 with series=[{bucket: "<UTC month>-01T00:00:00Z", value: 40.0, count: 2}];
   if the 48h backdate crosses a month boundary it is two buckets (30.0/count 1, then 10.0/count 1)
   — either way the bucket values sum to 40.0 and the bucket counts to 2
-> Note: measurements never surface in recall() — a recall(query="smoke_test_metric") returns only
   memories, so there is nothing to assert in the recall lane. The series is scoped to the
   temporary context and becomes unreachable with it in Cleanup (delete_context is a soft delete,
   so the rows themselves stay until a hard delete cascades — see Cleanup)
```

### 5. Memory update tools

```
update_memory(memory_id=..., context_id=..., summary="MCP smoke test memory — UPDATED", importance=0.7)
-> Verify: success response with memory_id, operation, re_embedded and scope
-> Verify (compact acknowledgement): the `persistence` block has no `detail` key

update_memory(memory_id=..., context_id=..., importance=0.8, verbose=true)
-> Verify: success response; persistence.detail is a non-empty string (verbose acknowledgement)

recall(context_id=..., query="smoke test UPDATED", k=5)
-> Verify: returns updated memory with new summary
-> Verify: its updated_at is now a UTC timestamp (it was null in step 4) and is not earlier than
   created_at
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
   target_id == <memory_id> (the linked memory) with origin="declared", confidence=1.0 and
   weight >= 1.0 (exactly 1.0 until a recall co-activates both ends — see the note)
-> Note (#741/#925): the linked_memory_ids declared link is stored as origin="declared" with
   edge_type="neural_association" — NOT a "declared_link" edge_type (that discriminator was removed
   in #741, which pivoted to the relation/origin two-axis model). MCP list_edges exposes the origin
   axis (declared / hebbian / semantic), so assert on origin + confidence of the edge to the linked
   target — do NOT assert that it is the only edge: a freshly remembered memory is not edge-free,
   hebbian/semantic auto-edges to similar memories (weight 0.25, confidence 0.5) appear within ~1 s
   of remember, so count is usually > 1. The declared edge also receives Hebbian weight bumps once a
   recall co-activates both endpoints (1.036 observed after a recall ran in the same batch), which is
   why the Verify line is weight >= 1.0 rather than == 1.0. (reference() also carries the declared-link
   surface as outgoing_links/incoming_links (#440); list_edges is used here because it is the
   origin-bearing view.)
```

```
create_edge(context_id=..., source_id=<memory_id>, target_id=<memory_id_2>, edge_type="related_to")
-> Verify: returns edge with edge_type="related_to", weight=1.0 (the schema default — not 0.5),
   confidence=1.0 and origin="declared"
-> Verify: operation is "created", OR "updated" together with a `previous` pre-image
   ({edge_type, weight, confidence, origin}) when an automatic (hebbian or semantic) edge already
   existed for this pair — #1321 promotes it to origin="declared" instead of failing. Both outcomes
   PASS; only "unchanged" or an `edge_exists` error would mean a stale declared edge from an earlier run

list_edges(context_id=..., memory_id=<memory_id>)
-> Verify: returns edges array with count >= 1, including the related_to edge to <memory_id_2>;
   every edge carries the origin field

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
-> Save the first binding's key_id (a string: akey_ + 22 letters/digits) as key_id, if any

describe_binding(key_id=<key_id from list_my_bindings>)
-> Verify: if list_my_bindings returned >= 1 binding, describing it succeeds with
   binding {key_id, name, context_id, context_name, created_at, key_prefix} (no secret material)
-> Verify: if no bindings exist, call describe_binding(context_id="00000000-0000-0000-0000-000000000000")
   instead and expect the uniform `binding_not_found` error (no side effects either way)
-> Note: the selectors are key_id (akey_...) OR context_id (UUID), exactly one of them — there is no
   binding_id parameter. An unknown or not-yours selector always returns binding_not_found, never
   a permission_denied that would leak whether the key exists. An integer key_id (the format before
   #1008) returns invalid_arguments
```

### 6.7. Batch write and deterministic reads (remember_batch / list / changes_since / bootstrap)

The v0.93.0 tools beside `remember` and `recall`: several memories in one call, and three reads
that neither rank nor embed.

```
remember_batch(context_id=..., tags_normalize=true, dedupe="off", items=[
  {"summary": "MCP smoke test batch item 1 — canonical tag", "content": "First item of the smoke-test batch.", "type": "note", "tags": ["smoke-test", "batch"]},
  {"summary": "MCP smoke test batch item 2 — tag variant", "content": "Second item: its tag differs from the stored spelling only by case and separator.", "type": "note", "tags": ["Smoke_Test", "batch"]},
  {"summary": "MCP smoke test batch item 3 — untagged", "content": "Third item: no tags, to check that recall omits the tags key.", "type": "note"}
])
-> Verify: status="success", count=3, succeeded=3, candidates=0, failed=0
-> Verify: every results[i] is {index: i, status: "success", memory_id, scope, persistence} with no
   persistence.detail (the batch takes verbose=true like remember)
-> Verify: results[1].lint has an entry with code="tag_normalized", subject="Smoke_Test",
   replacement="smoke-test" — tags_normalize stored the established spelling
-> Verify: the context fields (context_id, context_name, ...) are at the top level
-> Save results[0..2].memory_id as batch_id_1, batch_id_2 and batch_id_3
-> Note: dedupe="off" writes without ever suggesting a near-duplicate later; items 1 and 2 are
   near-identical on purpose and both must be written (no duplicate_candidate)

remember_batch(context_id=..., atomic=true, items=[
  {"summary": "MCP smoke test atomic item — must not be written", "content": "Valid item of a refused atomic batch.", "type": "note"},
  {"summary": "MCP smoke test atomic item — invalid (no content, no type)"}
])
-> Verify: status="error", error="batch_refused"; count=2, succeeded=0, failed=1, skipped=1 — the
   counters follow the per-item status, so the valid item the refusal never attempted counts as
   skipped, not as failed (the batch is refused before any write — this is not the post-write
   atomic rollback, whose items say "batch rolled back")
-> Verify: results[0].status="skipped" and results[1] is status="error", error="missing_fields";
   no result carries a memory_id — atomic=true wrote nothing (the list call below confirms it)

recall(context_id=..., query="smoke test batch item untagged", k=10)
-> Verify: the result for batch_id_3 has NO `tags` key (absent when empty, not []), while the
   results for batch_id_1 / batch_id_2 carry tags including "smoke-test" and "batch"

list(context_id=...)
-> Verify: status=success; order_by="updated_at" and direction="desc" (the defaults)
-> Verify: total=9 on a clean run (5 from step 3, memory 2 from step 6, 3 batch items — the refused
   atomic batch added none) and count == total; has_more=false, next_cursor=null
-> Verify: each item is {memory_id, summary, type, importance, scope, tags, delivery_mode,
   source_type, created_at, updated_at} with no `details` (opt-in with include_details=true);
   unlike recall, `tags` is always present here ([] for batch_id_3)
-> Verify: the item for <memory_id> has a non-null updated_at (edited in step 5); the three batch
   items have updated_at=null

list(context_id=..., filters={"tags": ["batch"]})
-> Verify: total=2 — exactly batch_id_1 and batch_id_2
-> Verify: batch_id_2's tags hold "smoke-test", not "Smoke_Test" (the normalized spelling is what
   was stored)

list(context_id=..., filters={"tags": ["batch"]}, order_by="created_at", direction="asc", limit=1)
-> Verify: count=1, total=2, has_more=true and next_cursor="1"
-> Save the returned memory_id as first_page_id

list(context_id=..., filters={"tags": ["batch"]}, order_by="created_at", direction="asc", limit=1, cursor="1")
-> Verify: count=1, has_more=false, next_cursor=null; the memory_id differs from first_page_id, so
   the two pages together are batch_id_1 and batch_id_2

list(context_id=..., filters={"near": {"lat": 35.6812, "lon": 139.7671}})
-> Verify: status="error", error="validation_error"; the message says `near` is not supported by
   list and points at recall_nearby

forget(memory_id=<batch_id_3>, context_id=...)
-> Verify: success response, deleted_count=1 (gives changes_since a "forgotten" event)

changes_since(context_id=..., since="2020-01-01T00:00:00Z")
-> Verify: status=success; `changes` is ordered oldest first (`at` ascending); each change is
   {memory_id, kind, at, summary}, except that every event of the forgotten batch_id_3 — its
   earlier "created" one included — carries no `summary` (forget retracts the text from this
   lane too, #1876); has_more=false, next_cursor=null, until=null
-> Verify: there is a kind="created" change for <memory_id>, batch_id_1, batch_id_2 and batch_id_3
   (a forgotten memory keeps its earlier events), a kind="updated" change for <memory_id> (the
   step 5 edit) and a kind="forgotten" change for batch_id_3
-> Verify: no kind="updated" change for batch_id_3 — it was never edited, and forget dates the
   delete by its "forgotten" event only (#1924)
-> Verify: count >= 11 on a clean run (9 created, at least 1 updated, 1 forgotten)

changes_since(context_id=..., since="2020-01-01T00:00:00Z", kinds=["forgotten"])
-> Verify: count=1 and the only change is kind="forgotten" for batch_id_3

changes_since(context_id=..., since="2020-01-02T00:00:00Z", until="2020-01-01T00:00:00Z")
-> Verify: status="error", error="validation_error", message "until must be later than since."

bootstrap(context_id=...)
-> Verify: status=success, degraded=false; `context` and `instructions` are present; `since` is
   about 7 days before `generated_at` (the default window)
-> Verify: components has exactly pinned, upcoming and changes, each with status="ok"
-> Verify: components.pinned.memories contains pinned_memory_id, with total_available, truncated=false
   and cap; components.upcoming.results contains time_memory_id; components.changes.changes is
   non-empty and components.changes.has_more=false
-> Verify: `guardrails` is a non-null object ({provenance, items, total_available, truncated,
   tool_triggered_version}); skip this check when the MCP URL carries `?guardrails=off` (the key
   is then absent)
-> Note: here `degraded` is true only when a component failed ({status: "error", error}); a
   degraded reply is a FAIL for that component, not for the tool

bootstrap(context_id=..., include=["pinned"], since="1d")
-> Verify: components has only `pinned`; `since` is about one day before `generated_at`
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

**Pre-condition:** all four read tools (`list_analyses`, `get_active_analysis`, `get_analysis`, `get_cluster`) require the workspace owner role and the analysis feature to be available, and that gate runs before any lookup. Valid responses therefore include gate errors (`permission_denied`, `feature_not_available`) on every row that sends a well-formed id — the smoke test should treat these as acceptable outcomes, not failures. Only the malformed-id rows are role-independent: the id is parsed before the gate.

```
list_analyses(context_id=...)
-> Verify: returns items array, or gate error (`permission_denied`, `feature_not_available`) — treat all as PASS

get_active_analysis(context_id=...)
-> Verify: returns analysis run object, `no_succeeded_run`, or gate error (`permission_denied`, `feature_not_available`) — treat all as PASS

get_analysis(run_id="00000000-0000-0000-0000-000000000000")
-> Verify: returns `run_not_found` (expected — fake run_id), or the gate error
   (`permission_denied`, `feature_not_available`) for a caller the gate refuses — treat all as PASS

get_analysis(run_id="this-is-not-a-uuid")
-> Verify: returns `invalid_uuid` for every caller (the id is parsed before the gate)
```

```
get_cluster(run_id="00000000-0000-0000-0000-000000000000", cluster_index=0)
-> Verify: returns `cluster_not_found` (expected — fake run_id), or the gate error
   (`permission_denied`, `feature_not_available`) for a caller the gate refuses — treat all as PASS

get_cluster(run_id="this-is-not-a-uuid", cluster_index=0)
-> Verify: returns `invalid_uuid` for every caller (the id is parsed before the gate)
```

### 7.7. Sleep tools

Note: `get_sleep_history` and `get_sleep_report` are read-only inspection tools. `rollback_sleep_run` is mutating — fake IDs verify error handling without side effects.

```
get_sleep_history(context_id=...)
-> Verify: returns `{reports: [...], count: ...}` (no error; may be empty); each report carries the
   run counters incl. `llm_call_failures` (the magnitude behind a degraded/failed status, #1183)

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

### 7.8. Resource tools (`resources` plan feature only)

**Pre-check:** `get_usage()` reports the plan *name* only, not the effective feature set, so treat it as a hint and let `setup_resource` be the probe. Since #1551 resource *creation* is gated on the `resources` plan feature, which only XL (`promax`) carries by default; operators can move it between tiers with `PLAN_<KEY>_FEATURES`, so the plan name alone cannot tell you whether the gate is open — do not skip this section on the name. Attempt `setup_resource` (P1); if it refuses with the plan gate — `plan_required`, with `required_plan` naming the lowest tier that carries the feature (or `null` when an override removed it from every tier); the REST equivalent is `FEAT-001` — record P1–P7 as SKIP, not FAIL, and note "Resource tools skipped — plan lacks the `resources` feature (XL / `promax` by default)" in the report. Lower tiers keep serving resources that already exist; only creating new ones is gated.

The role check comes first: `setup_resource` (like `list_resource_tokens`) is owner/admin only and answers `permission_denied` with `required_role="owner or admin"` before it looks at the plan. That refusal is also a SKIP for P1–P7 ("Resource tools skipped — owner/admin required"), not a FAIL.

```
setup_resource(name="smoke-test-resource-{unix_timestamp}", resource_id="smoke_test_{unix_timestamp}")
-> Verify: returns context_id (UUID), resource_id, token (plaintext), token_id (rtok_ + 22 letters/digits)
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

list_resource_tokens(resource_id=<resource_id>, include_revoked=false)
-> Verify: returns a tokens array with exactly one token: id == the token_id saved from
   setup_resource, resource_id matching and is_active=true (the plaintext token is never listed)
```

**Resource cleanup** (runs even if some steps failed). `delete_context` does not revoke the
resource token: the token stays active and keeps authenticating ingest calls for the resource, and
the resource stays in the Web UI resource list with a "Context deleted" marker for as long as it
holds an active token. There is no MCP tool that revokes a resource token, so revoke it outside
MCP **before** deleting the context — once the context is gone, `list_resource_tokens` with this
`resource_id` answers `resource_not_found` and the token can only be found in the Web UI:

```
Revoke the token (workspace owner only — not an MCP call)
-> Either: Web UI → Workspace → Resources → the smoke-test resource → its tokens tab → revoke
-> Or: DELETE /api/v1/resource-tokens/<token_id> with the owner's REST credentials → 204
-> Verify: list_resource_tokens(resource_id=<resource_id>, include_revoked=false) now returns an
   empty tokens array (the revoked token is still listed with is_active=false when
   include_revoked is left at its default)
-> If the run cannot revoke it (no owner role, no REST credentials, nobody at the Web UI): record
   P6 as MANUAL and print the token_id and resource_id in the report so the owner can revoke it —
   never report the resource rows as cleaned up while the token is active

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
-> Verify: status=success with the default context and the component results
-> Verify: when `degraded` is true, name the cause in the report — either a component with
   status="error", or a component that is ok but impaired: the recall component carries its own
   degraded=true with a degraded_reason when it was served without the semantic arm. Both are a
   PASS for the tool call (the bootstrap is fail-soft by contract); a degraded=true that no
   component explains is a FAIL

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
agent-state entry (set_state) needs no explicit delete — it becomes unreachable with its
soft-deleted context below and also expires via its TTL; the measurement series (record_measurement)
is likewise scoped to the context and has no delete tool of its own. Note that delete_context is a
soft delete (it sets deleted_at on the context and its memories and hard-deletes only the neural
edges), so the agent_states and measurements rows stay in their tables until a hard delete cascades.

```
unbind_agent_context(agent_id=<agent_id>, binding_id=<agent_binding_id>)
-> Run only if Agent Control Plane cleanup did not already remove the binding

delete_agent(agent_id=<agent_id>)
-> Run only if Agent Control Plane cleanup did not already remove the agent

update_memory(memory_id=<pinned_memory_id>, context_id=..., delivery_mode="on_recall")
-> Verify: success response (pinned memory unpinned — no longer deterministically loaded)

forget(memory_id=<guardrail_memory_id>, context_id=...)
-> Verify: success response, deleted_count=1 (the editor who wrote it may delete it)

forget(memory_id=<pinned_memory_id>, context_id=...)
-> Verify: success response (pinned memory deleted)

delete_context(context_id=<merge_target_id>)
-> Verify: success response (merge target soft-deleted, along with its memories)

forget(memory_id=<memory_id_2>, context_id=...)
-> Verify: success response (memory 2 deleted from source)

delete_context(context_id=...)
-> Verify: success response (source context soft-deleted, along with its memories — the agent-state
   entries and the smoke_test_metric series are not deleted, they go unreachable behind the
   soft-deleted context: get_state / recall_series on it now return `context_not_found`)
```

### 8. Coverage cross-check (anti-drift)

Reconcile this skill against the canonical registry so the "all MCP tools" claim cannot silently
rot. The source of truth is `backend/src/mcp_server/tools/_definitions.py` (**70 tools**). Every
registered tool must be in exactly one group below. **If the registry and these groups disagree,
the skill is out of date — add a row (or a documented exclusion) before merging.**

Two checks keep this honest:

- **In the run:** step 0's `describe_tools()` — `len(listed) + count` is the live server's registry
  size and must equal 70, the number these groups are built for. It replaces the old `grep -c` on
  `_definitions.py`, which needed a repository checkout and said nothing about the server under test.
- **In CI:** `backend/tests/mcp_server/test_smoke_test_runbook.py` parses the groups below and fails
  when a registered tool is in none of them, when a group's stated size or the total differs from
  the registry, or when a tool listed as exercised has no call in the steps.

"Inline" below means "called by every run"; it has nothing to do with the server's `core` tool
profile (the default `tools/list` view), which is why the run needs `?profile=full`.

**Exercised inline (41 tools):** `describe_tools`, `guide`, `list_contexts`, `create_context`,
`get_context_info`, `update_context`, `update_search_config`, `remember`, `remember_batch`,
`recall`, `recall_upcoming`, `recall_nearby`, `load_pinned`, `load_guardrails`, `feedback`,
`set_state`, `get_state`, `reference`, `explore`, `update_memory`, `forget`, `list`,
`changes_since`, `bootstrap`, `list_edges`, `create_edge`, `update_edge`, `delete_edge`,
`list_tags`, `list_my_bindings`, `describe_binding`, `merge_contexts`, `get_usage`,
`list_analyses`, `get_active_analysis`, `get_analysis`, `get_cluster`, `get_sleep_history`,
`get_sleep_report`, `rollback_sleep_run`, `delete_context`.

**Exercised inline — measurement lane (2 tools):** `record_measurement`, `recall_series`.

**Exercised with owner/admin role, else SKIP (10 tools):** `register_agent`, `list_agents`,
`get_agent`, `update_agent`, `delete_agent`, `bind_agent_context`, `list_agent_bindings`,
`update_agent_binding`, `unbind_agent_context`, `get_agent_bootstrap`.

**Exercised only with the resources plan feature (XL by default) and the owner/admin role, else SKIP (5 tools):**
`setup_resource`, `ingest_events`, `get_resource_impact`, `get_resource_schema`,
`list_resource_tokens`.

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

41 + 2 + 10 + 5 + 12 = **70** — the full registry. The conditional rows are the 10 owner/admin Agent
Control Plane tools and 5 `resources`-gated (XL) tools; the remaining 12 are documented exclusions.

### 9. Report

Print a summary table (numbers are illustrative; the executed order follows the sections above):

```
## MCP Smoke Test Results

| # | Tool | Action | Result |
|---|------|--------|--------|
| 1 | describe_tools | Registry count 70, nothing hidden (else abort: reconnect with ?profile=full) | PASS/FAIL |
| 2 | describe_tools | Full definition by name + unknown name | PASS/FAIL |
| 3 | guide | Index topic | PASS/FAIL |
| 4 | guide | Tool name expands to sections + unknown topic | PASS/FAIL |
| 5 | list_contexts | List contexts (total = page length; count = quota usage) | PASS/FAIL |
| 6 | create_context | Create test context (top-level context_id) | PASS/FAIL |
| 7 | list_contexts (verify) | Find the new context with name_contains + include_summary | PASS/FAIL |
| 8 | get_context_info | Get context details | PASS/FAIL |
| 9 | update_context | Update display name | PASS/FAIL |
| 10 | update_search_config | Update search weights | PASS/FAIL |
| 11 | remember | Create test memory (source_uri, source_type; compact persistence, no detail) | PASS/FAIL |
| 12 | remember | Create time memory (verbose=true keeps persistence.detail) | PASS/FAIL |
| 13 | remember | Create located memory (details.location lat/lon) | PASS/FAIL |
| 14 | remember | Create pinned memory (delivery_mode="always") | PASS/FAIL |
| 15 | remember | Create tool guardrail memory (details.tool_trigger) | PASS/FAIL |
| 16 | remember | Reject a guardrail with a nested quantifier (validation_error) | PASS/FAIL |
| 17 | recall | Search for memory (tags present, updated_at null) | PASS/FAIL |
| 18 | list_tags | Unknown argument refused with invalid_argument (SKIP if the client validates first) | PASS/FAIL/SKIP |
| 19 | recall | Search with include_explore_hints=true | PASS/FAIL |
| 20 | recall | Search with source_uri_prefix filter | PASS/FAIL |
| 21 | recall | Search with source_type filter | PASS/FAIL |
| 22 | recall | Search with trust_tier="trusted" filter | PASS/FAIL |
| 23 | reference | Get full memory | PASS/FAIL |
| 24 | explore | Graph traversal | PASS/FAIL |
| 25 | recall_upcoming | List upcoming time memories | PASS/FAIL |
| 26 | recall_nearby | Spatial query around the located memory (distance_m, stored location with label) | PASS/FAIL |
| 27 | load_pinned | Deterministic load of pinned set | PASS/FAIL |
| 28 | load_guardrails | Deterministic load of the guardrail set (format=1, version, normalized tool_trigger) | PASS/FAIL |
| 29 | feedback | Record helpful signal on a recall result | PASS/FAIL |
| 30 | set_state | Set + overwrite agent run-state (TTL) | PASS/FAIL |
| 31 | get_state | Read one key + list all live keys (has_more / next_cursor) | PASS/FAIL |
| 32 | record_measurement | Append observation (measured_at defaults to now) | PASS/FAIL |
| 33 | record_measurement | Append backdated observation (measured_at -48h) | PASS/FAIL |
| 34 | recall_series | Default day/avg — one bucket per UTC day, count=2 | PASS/FAIL |
| 35 | recall_series | period=month agg=sum — values sum to 40.0 | PASS/FAIL |
| 36 | update_memory | Update memory (compact persistence, no detail) | PASS/FAIL |
| 37 | update_memory | Update with verbose=true (persistence.detail) | PASS/FAIL |
| 38 | recall (verify) | Verify update (new summary, updated_at set) | PASS/FAIL |
| 39 | remember | Create 2nd memory (linked_memory_ids, linked_source_uris) | PASS/FAIL |
| 40 | list_edges (verify) | Verify declared link (origin="declared", confidence 1.0, weight >= 1.0) | PASS/FAIL |
| 41 | create_edge | Create test edge (weight 1.0 default; created or #1321 updated) | PASS/FAIL |
| 42 | list_edges | List edges (origin exposed) | PASS/FAIL |
| 43 | update_edge | Update edge weight | PASS/FAIL |
| 44 | delete_edge | Delete edge | PASS/FAIL |
| 45 | list_tags | List tags in context | PASS/FAIL |
| 46 | list_tags | List tags with prefix filter | PASS/FAIL |
| 47 | list_my_bindings | List owner-scoped API-key bindings | PASS/FAIL |
| 48 | describe_binding | Describe a binding by key_id (or fake context_id → binding_not_found) | PASS/FAIL |
| 49 | remember_batch | 3 items with tags_normalize + dedupe="off" (tag_normalized lint) | PASS/FAIL |
| 50 | remember_batch | Refused atomic=true batch (batch_refused, nothing written) | PASS/FAIL |
| 51 | recall (verify) | Untagged batch item has no tags key | PASS/FAIL |
| 52 | list | Default order (updated_at desc), total=9 | PASS/FAIL |
| 53 | list | tags filter (normalized spelling stored) | PASS/FAIL |
| 54 | list | Paging with limit=1 + cursor | PASS/FAIL |
| 55 | list | Refused filter (near → validation_error) | PASS/FAIL |
| 56 | forget | Delete batch item 3 (seeds a forgotten change) | PASS/FAIL |
| 57 | changes_since | Created / updated / forgotten kinds, oldest first | PASS/FAIL |
| 58 | changes_since | kinds=["forgotten"] only | PASS/FAIL |
| 59 | changes_since | until guard (until <= since → validation_error) | PASS/FAIL |
| 60 | bootstrap | Default: pinned + upcoming + changes, degraded=false | PASS/FAIL |
| 61 | bootstrap | Narrowed include=["pinned"], since="1d" | PASS/FAIL |
| 62 | create_context | Create merge target context | PASS/FAIL |
| 63 | merge_contexts | Merge source into target | PASS/FAIL |
| 64 | get_usage | Get workspace usage | PASS/FAIL |
| 65 | list_analyses | List analysis runs (or gate error) | PASS/FAIL |
| 66 | get_active_analysis | Get latest succeeded analysis (or gate error) | PASS/FAIL |
| 67 | get_analysis | Fake run_id (run_not_found or gate error) + malformed id (invalid_uuid) | PASS/FAIL |
| 68 | get_cluster | Fake run_id (cluster_not_found or gate error) + malformed id (invalid_uuid) | PASS/FAIL |
| 69 | get_sleep_history | Get sleep maintenance history | PASS/FAIL |
| 70 | get_sleep_report | Get sleep report (fake ID, error handling) | PASS/FAIL |
| 71 | rollback_sleep_run | Rollback sleep run (fake ID, error handling) | PASS/FAIL |
| 72 | update_memory | Unpin pinned memory (delivery_mode="on_recall") | PASS/FAIL |
| 73 | forget | Delete guardrail memory | PASS/FAIL |
| 74 | forget | Delete pinned memory | PASS/FAIL |
| 75 | delete_context | Soft-delete merge target and its memories | PASS/FAIL |
| 76 | forget | Delete memory 2 | PASS/FAIL |
| 77 | delete_context | Soft-delete source context (agent-state + measurement series go unreachable) | PASS/FAIL |
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
| P1 | setup_resource | Create resource context + token (XL only) | PASS/FAIL/SKIP |
| P2 | ingest_events | Batch ingest 2 test events (XL only) | PASS/FAIL/SKIP |
| P3 | get_resource_impact | Get resource stats (XL only) | PASS/FAIL/SKIP |
| P4 | get_resource_schema | Get schema (expect not_found) (XL only) | PASS/FAIL/SKIP |
| P5 | list_resource_tokens | List the active token for the resource (XL only) | PASS/FAIL/SKIP |
| P6 | (REST / Web UI) | Revoke the resource token, then list again → no active token (XL only) | PASS/FAIL/MANUAL/SKIP |
| P7 | delete_context | Delete resource context (XL only) | PASS/FAIL/SKIP |

**Result: N/77 main rows passed** (+ N/10 Agent Control Plane rows and N/7 XL resource rows
passed, or SKIP when the corresponding gate is unavailable)

Note: the 77 numbered rows are test *steps*, not distinct tools — several tools (describe_tools,
guide, remember, remember_batch, recall, list, changes_since, bootstrap, update_memory, forget,
delete_context, list_contexts, create_context, list_edges, list_tags, record_measurement,
recall_series) are exercised in multiple rows. Distinct-tool coverage is reconciled in the
Coverage cross-check (41 inline + 2 measurement + 10 owner/admin + 5 XL + 12 documented-skip = 70).
"Main rows" are the rows every run executes; the name is unrelated to the server's `core` profile.

Test context: smoke-test-{timestamp} (cleaned up)
Tool list: full (describe_tools: 70 listed, 0 hidden)
Resource token: revoked | MANUAL — token_id rtok_..., resource_id smoke_test_{timestamp} still active | not created (resource rows skipped)

Documented exclusions / gated-skip (see Coverage cross-check) — not counted as FAIL:
- analyze_context (billing + BYOK + owner + Pro-tier)
- Agent Control Plane tools may be role-gated (owner/admin); if skipped, list A1–A10 as SKIP
- Resource tools are role-gated (owner/admin) and plan-gated (`resources`); if skipped, list P1–P7 as SKIP
- setup_connector (external connector credentials + live target)
- File tools: init_file_upload, complete_file_upload, get_file_download_url, delete_file, list_files (multipart S3/R2)
- Secret tools: secret_register_pubkey, secret_put, secret_get, secret_list, secret_revoke_grant (zero-knowledge: age keypairs + owner approval + ciphertext)

Registry reconciliation: 41 inline + 2 measurement + 10 owner/admin + 5 XL + 12 documented-skip = 70 tools in _definitions.py.
```

If any step fails:
- Mark it as FAIL with error message
- **Continue** with remaining steps where possible (skip dependent steps)
- Still attempt cleanup even if earlier steps failed (including unpinning the pinned memory and
  revoking the resource token)
- A step 0 abort is the one exception to "continue": nothing was created, so there is nothing to
  clean up — report the reconnect instruction and stop
- Show total pass/fail count in summary
