# MCP Tools Reference

See [MCP Client Setup](mcp-clients.md) for connecting a client, and [Core Concepts](concepts.md) for the memory model behind these tools.

63 tools across 13 categories. Workspace roles: **Owner** > Admin > Member > **Viewer** (read-only). Context roles: **Owner** > Editor > Viewer. Private contexts are visible only to the creator. Members may be restricted to specific contexts via allowlist.

## Memory (7)

| Tool | Description | Required Role |
|------|------------|---------------|
| `remember` | Store a new memory (summary + content + type; optional `delivery_mode`) | Member+ |
| `recall` | Search memories with Hybrid Search (supports `trust_tier` filter) | Viewer+ |
| `recall_nearby` | Deterministic WHERE-axis query — memories with `details.location` within `radius_m` of a point, nearest first | Viewer+ |
| `reference` | Get full 3-layer details of a memory | Viewer+ |
| `update_memory` | Update an existing memory in-place or upsert by external ID | Member+ |
| `forget` | Soft-delete a memory (30-day retention) | Member+ |
| `explore` | Discover related memories via Neural Memory graph | Viewer+ |

## Agent Substrate (7)

The primitives an autonomous agent loop needs beyond a knowledge store — see [Concepts › Agent Memory Substrate](concepts.md#agent-memory-substrate).

| Tool | Description | Required Role |
|------|------------|---------------|
| `load_pinned` | Deterministically load always-load memories (`delivery_mode="always"`) — Goal / Guardrail / policy | Viewer+ |
| `recall_upcoming` | List upcoming Time Memories (`type="time"`, `delivery_mode="on_trigger"`) | Viewer+ |
| `set_state` | Upsert agent scratch state (key→value, optional TTL; excluded from recall) | Editor+ |
| `get_state` | Read one state key, or list all live state for a context | Viewer+ |
| `record_measurement` | Append one numeric observation to a metric's series (HOW-MUCH lane; excluded from recall, untouched by Sleep) | Editor+ |
| `recall_series` | Read a metric's series bucketed by day/week/month with avg/min/max/sum/count/last | Viewer+ |
| `feedback` | Record whether a recalled memory was helpful (append-only signal) | Viewer+ |

## Agent Control Plane (10, preview)

The v0.49.0 control plane builds on existing workspace RBAC: agents are registry resources, not principals, and context bindings can only remove access from an agent-bound member key. Registry, bindings, composed bootstrap, W3C Trace Context/baggage correlation, and the append-only audit foundation are implemented.

| Tool | Description | Required Role |
|------|------------|---------------|
| `register_agent` | Register a workspace-scoped agent | Owner/Admin |
| `list_agents` | List registered agents and lifecycle/enforcement status | Owner/Admin |
| `get_agent` | Get one registered agent | Owner/Admin |
| `update_agent` | Update metadata, `status`, or `enforcement_mode` | Owner/Admin |
| `delete_agent` | Permanently delete an agent and its bound keys; prefer `status="retired"` operationally | Owner/Admin |
| `bind_agent_context` | Add a purely subtractive context binding | Owner/Admin |
| `list_agent_bindings` | List an agent's context bindings | Owner/Admin |
| `update_agent_binding` | Update read/write/default binding policy | Owner/Admin |
| `unbind_agent_context` | Remove a binding (default-deny in enforce mode) | Owner/Admin |
| `get_agent_bootstrap` | Compose context guide + pinned + optional trusted recall + upcoming + state for session start | Agent-bound key or Owner/Admin |

> **Preview boundary:** per-memory type/source filters are enforced on the memory-read lanes (recall, recall_nearby, reference, forget, explore, load_pinned, upcoming) for enforce-mode agents as of [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299) — and on the enumeration/aggregate surfaces (list, stats, access-patterns, get_cluster) as of [#1301](https://github.com/kagura-ai/memory-cloud/issues/1301) — `null` = all types, `[]` = deny-all; shadow mode records `would_deny` without filtering. `traceparent` plus W3C baggage correlation for `agent_id` / `session_id` / `run_id` is implemented, but server-side span export remains out of scope for P0. `memory_access_events` is live for bootstrap, load-pinned, feedback, recall, reference, remember, update, and forget with binding deny / `would_deny` persistence.

## Neural Edges (4)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_edges` | List edges connected to a memory | Viewer+ |
| `create_edge` | Create an edge between two memories | Member+ |
| `update_edge` | Update edge weight or type | Member+ |
| `delete_edge` | Delete an edge between two memories | Member+ |

## Contexts (7)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_context_info` | Get context metadata and guidelines | Viewer+ |
| `list_contexts` | List available contexts in workspace | Viewer+ |
| `create_context` | Create a new context | Owner/Admin |
| `update_context` | Update context settings (summary, usage guide, resource_id, is_public) | Editor+ |
| `delete_context` | Delete a context and all its memories | Owner/Admin |
| `merge_contexts` | Merge memories from source context into target context | Owner/Admin |
| `update_search_config` | Tune hybrid search weights, reranker settings, and query-intent routing (`routing_mode`) per context | Editor+ |

## Tags (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_tags` | List tag vocabulary in a context (call before remember/recall to align tagging) | Viewer+ |

## Files / R2 Attachments (5)

| Tool | Description | Required Role |
|------|------------|---------------|
| `init_file_upload` | Reserve quota + return presigned PUT URL (R2, ≤100 MiB) | Member+ |
| `complete_file_upload` | Finalize upload after R2 PUT, verify sha256, mark as uploaded | Member+ |
| `list_files` | List uploaded, non-deleted files in the workspace (newest first) | Viewer+ |
| `get_file_download_url` | Issue presigned GET URL for a file | Viewer+ |
| `delete_file` | Soft-delete a file object | Member+ |

## Analyses — Memory Analysis (5)

Cluster memories into themes (kouchou-ai-style UMAP + KMeans + LLM labeling) for large-scale qualitative analysis.

| Tool | Description | Required Role |
|------|------------|---------------|
| `analyze_context` | Start an analysis run (or preview cost with `dry_run=true`) | Owner + Pro plan + BYOK + quota |
| `list_analyses` | List past analysis runs for a context | Owner |
| `get_analysis` | Get a completed analysis (clusters, labels, stats) | Owner |
| `get_active_analysis` | Get the in-flight analysis for a context (if any) | Owner |
| `get_cluster` | Drill into a single cluster's member memories | Owner |

Analysis runs are capped by `ANALYSIS_MAX_MEMORY_COUNT` (default 10,000; preview and start reject larger contexts). Since v0.47.0, cancellation is all-or-nothing, deleted-context runs are invisible on both REST and MCP, strict BYOK labeling never falls back to the platform key, and runs fail when more than half of labelable clusters fail labeling.

## Resources — External Data Ingestion (6)

| Tool | Description | Required Role |
|------|------------|---------------|
| `setup_resource` | Create public context + issue resource token | Owner/Admin + Pro plan |
| `setup_connector` | Provision an ai-worker chat connector (resource + connector row + token) | Owner/Admin + connector seats |
| `list_resource_tokens` | List active resource tokens for your workspace | Owner/Admin |
| `ingest_events` | Batch upsert/delete events into a resource (max 100 events; session-auth MCP variant) | Member+ |
| `get_resource_impact` | Resource stats (tokens, memories, schema version) | Viewer+ |
| `get_resource_schema` | Field definitions for a resource | Viewer+ |

## Secrets (5)

Zero-knowledge secret store: the server holds only `age` public recipient keys and opaque ciphertext, and **never decrypts**. Encryption/decryption happen client-side (the `kagura secret` CLI / SDK). `list` returns metadata only — there is no endpoint that returns a plaintext value.

| Tool | Description | Required Role |
|------|------------|---------------|
| `secret_register_pubkey` | Register your own `age` recipient public key (starts pending; an owner approves it before it can receive grants) | Member+ |
| `secret_put` | Store age-encrypted ciphertext + grant approved recipients (`recipients_snapshot` must match `grant_pubkey_ids`) | Owner/Admin |
| `secret_get` | Fetch ciphertext you hold an active grant for (decrypt locally; every fetch is recorded in a tamper-evident audit log) | Member+ |
| `secret_list` | List secret names + metadata (status, version, grant count, rotation flag) — never values | Owner/Admin |
| `secret_revoke_grant` | Revoke a recipient's grant and flag the secret `rotation_needed` (not retroactive — rotate upstream) | Owner/Admin |

## Sleep Maintenance (3)

Background consolidation of memories (decay, edge pruning, theme summarization).

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_sleep_history` | List past sleep runs | Viewer+ |
| `get_sleep_report` | Detailed sleep report with all actions | Viewer+ |
| `rollback_sleep_run` | Rollback all actions from a completed sleep run | Member+ (report owner only) |

## Usage (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_usage` | Get current workspace usage (memories, contexts, members, MCP calls/day) | Viewer+ |

## API-Key Bindings (2)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_my_bindings` | List your public-bound API keys (read-only; owner-scoped) | Viewer+ |
| `describe_binding` | Describe one binding by `key_id` XOR `context_id` (read-only; owner-scoped) | Viewer+ |
