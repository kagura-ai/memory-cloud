# Design sign-off: `get_agent_bootstrap` composed contract (RFC-0002 F2)

- **Status**: Signed off (gating design for P0-3 implementation)
- **Issue**: [#1259](https://github.com/kagura-ai/memory-cloud/issues/1259) — gating item F2 of RFC-0002
  (Agent Memory & Context Control Plane; RFC text maintained locally, lands in
  `docs/rfc/0002-agent-memory-context-control-plane.md` when published)
- **Consumers**: implementers of P0-3 (`get_agent_bootstrap` MCP tool + REST companion),
  SDK maintainers (parity follow-up)
- **Depends on**: [Agent Registry & Context Bindings](agent-registry-and-bindings.md) (F1, #1258)
  for agent/binding resolution

`get_agent_bootstrap` is the single session-start call that rehydrates an agent's cognitive
state: context metadata and usage guide, always-load pinned memories, a trusted-only semantic
recall, upcoming time memories, and the agent-state lane (plus a P1 policy-revision pointer).
It is modeled on the existing composed-read precedent `get_context_info`
(`backend/src/mcp_server/tools/context.py`): resolve context, gather from N services, return
one envelope with embedded instructions.

## Design invariants (normative)

1. **Pure composition at the service layer — no parallel retrieval path.** Every component
   delegates to the exact service chokepoint the standalone primitive uses, so bounds,
   ordering, trust filtering, ranking (including reinforcement re-rank), and IDOR posture are
   **inherited, not re-specified**. A precomputed "bootstrap bundle" table and a new batched
   query engine were both rejected (parallel path, staleness, second scoring surface).
2. **`agent_id` is REQUIRED; agent-less clients are untouched.** Existing MCP/REST clients
   keep using `get_context_info`, `load_pinned`, `recall`, etc. unchanged. Making `agent_id`
   optional was rejected — it would invite "anonymous bootstrap" flows that bypass binding
   narrowing.
3. **The recall component is trusted-tier-only and not configurable in v1.** Bootstrap output
   is behaviour-establishing by definition — exactly the read class the trust filter was built
   for (OWASP LLM01/LLM03). An `include_external` flag and a per-binding override column were
   rejected; an agent that wants unfiltered recall calls `recall()` directly, which is
   separately audited.
4. **The bundle is advisory model-visible context.** It does not and cannot enforce anything;
   hard action allow/deny belongs to an external gateway/runtime. The `instructions` block
   carries the advisory rule that agents MUST NOT store credentials or secret material as
   memory content.

## Component composition table

| Component | Delegated primitive (chokepoint) | Inherited bounds / invariants |
|---|---|---|
| `context` + `instructions` | `_resolve_context_for_read` + context row + search-config fallback, as in `tools/context.py`; instructions from the static constant in `tools/_constants.py` plus the context `usage_guide` | uniform `context_not_found` on any deny (CWE-639); API-key workspace confinement via contextvar |
| `pinned` | `MemoryService.load_pinned` | `pinned_load_cap` default 100, clamp [1, 1000]; deterministic `importance DESC, created_at ASC, id ASC`; `truncated` + `total_available` never silent; partial columns (no `content`/`details`) |
| `recall` | `MemoryService` recall with `filters={"trust_tier": "trusted"}` | trusted-context subquery + `source_type != 'connector'` defence-in-depth; normal recall semantics incl. reinforcement re-rank and access counters — unchanged by design |
| `upcoming` | the `recall_upcoming` window-overlap query | `k` default 20, clamp [1, 100]; `from` is always `"now"` |
| `state` | `AgentStateService.list_state` | bounded structurally by one row per `(context_id, key)` upsert; expired rows reaped before return |
| `policy` | P1 pointer only | `null`/skipped in P0; reserved shape `{bundle_id, revision_id, revision, content_sha256}` |

Implementation note (descriptive): the `upcoming` query currently lives in the MCP handler;
composition hoists it into a shared service helper consumed by both the existing tool and the
bootstrap service — the `AgentStateService` dual-surface pattern (`tools/state.py` +
`routes/agent_state.py`). Same predicate, same clamps, no second implementation.

## Request contract

MCP tool (`readOnly: True`; strict schema — `additionalProperties: false` is centrally
stamped, and every read param must be declared per the schema policy test
`backend/tests/mcp_server/test_tool_schema_policy.py`):

```jsonc
{
  "agent_id": "uuid",            // REQUIRED. From the Agent Registry.
  "context_id": "uuid",          // optional; default = agent's default binding
  "session_id": "string",        // optional, opaque, <=128 chars, [A-Za-z0-9._-]; correlation only
  "query": "string",             // optional, <=1024; enables the recall component
  "recall_k": 10,                // optional; forwarded verbatim to recall's existing k validation
  "pinned_cap": 100,             // optional; clamped by the load_pinned clamp to [1, 1000]
  "upcoming_until": "ISO|null",  // optional; "from" is always "now"
  "include": ["pinned","recall","upcoming","state","policy"]  // optional selector; default all
}
```

Schema-audit requirement (from #990): before freezing the schema, audit the handler's
top-level `args.get()` keys against the declared `properties` — a read-but-undeclared param
under `additionalProperties: false` freezes a wrong contract.

**Default-context resolution.** If `context_id` is omitted, the server resolves the agent's
default binding — the row with `is_default = true`, or the agent's sole binding when exactly
one exists. If the agent has multiple bindings and no default, the call fails with
`context_id_required` — **without enumerating bindings in the error** (no existence oracle).

**REST companion.** `POST /api/v1/agents/{agent_id}/bootstrap` (body = the same fields minus
`agent_id`); router mounted bare with its own prefix per the epic-#885 style; auth is
`APIKeyOrSessionUser`; response models inherit `TZAwareBaseModel`. POST-for-read follows the
`POST /api/v1/memory/pinned` precedent.

## Response contract

```jsonc
{
  "status": "success",
  "degraded": false,                       // true if any component reports "error"
  "agent": {
    "agent_id": "…", "name": "…",
    "binding": { "context_id": "…", "is_default": true }
  },
  "context": { /* byte-compatible with get_context_info's context block */ },
  "instructions": "<usage_guide + standard instructions markdown>",
  "components": {
    "pinned":   { "status": "ok", "memories": [ /* load_pinned rows */ ],
                  "total_available": 12, "truncated": false, "cap": 100 },
    "recall":   { "status": "ok", "query_hash": "<hmac-sha256>",   // never the raw query
                  "results": [ /* recall rows */ ], "k": 10, "trust_filter": "trusted" },
    "upcoming": { "status": "ok", "results": [ /* recall_upcoming rows */ ], "from": "…", "until": "…" },
    "state":    { "status": "ok", "states": { "…": {} }, "count": 3 },
    "policy":   { "status": "skipped", "reason": "no_policy_bundle" }
  },
  "correlation": { "agent_id": "…", "session_id": "…", "trace_id": "…", "span_id": "…" },
  "generated_at": "<ISO-8601>"
}
```

**Component sub-envelopes are byte-compatible with the standalone primitives' response
shapes** (`load_pinned` → `{memories, total_available, truncated, cap}`; `recall_upcoming` →
`{results}`; keyless `get_state` → `{states, count}`). Clients reuse one parser per primitive
whether they call it directly or via bootstrap. A flattened bespoke bundle schema was
rejected (dual maintenance on every primitive change).

## Identity rule (normative)

- For **agent-bound keys**: the requested `agent_id` MUST equal the key's `agent_id`. Any
  mismatch returns uniform `agent_not_found` (nonexistent and not-yours are
  indistinguishable).
- For **credentials not bound to any agent**: only a workspace owner/admin MAY bootstrap an
  agent of their workspace (e.g. for testing), and such calls MUST record
  `event_metadata.on_behalf_of` with the acting `principal_type`, so audit rows minted by
  non-agent operators are distinguishable from agent activity and cannot masquerade as it.
- Authorization layering is strictly additive on the existing chain: first
  `PermissionService.resolve_context_for_workspace_read` with `key_workspace_id` forwarding
  (uniform 404) including `allowed_context_ids` semantics; only then the AgentContextBinding
  check, which MAY narrow and MUST NOT widen access.

## Error and partial-failure envelope

- Identity/authorization failures are **total and fail-closed**: `agent_not_found` (uniform),
  `context_not_found` (uniform, CWE-639), `context_id_required`, `invalid_arguments` — via
  the `_error_response` snake_case codes.
- **Component failures are fail-soft.** If context resolution and binding checks succeed, a
  failing component yields `{"status": "error", "error": "<code>"}` for that component only,
  `degraded: true` at the top level, generic message to the caller, full detail to
  `logger.error(..., exc_info=True)`. All-or-nothing was rejected: a transient vector-store
  error must not deny the agent its pinned memories and state, which are Postgres-only.
- The whole call runs under `execute_with_timeout` with a `TOOL_TIMEOUTS` entry
  (~15 s, `get_context_info` precedent).
- Error-path hygiene: the handler must `await db.rollback()` before returning an error
  envelope (the `get_db()` auto-commit trap; established review finding pattern).

## Rate limiting, side effects, registration (normative)

- **Rate-limit exemption is scoped to query-less calls only.** The tool joins
  `_RATE_LIMIT_EXEMPT_TOOLS` with a justification comment that MUST state the query-scoped
  condition. Rationale: the cited exemption precedents (`get_context_info`, `load_pinned`)
  are exempt precisely because they carry no embedding/LLM cost; a query-less bootstrap
  matches them (Postgres-only deterministic reads). A query-carrying bootstrap runs its
  recall component under the **normal recall rate accounting**; on limit, that component
  degrades to `{"status": "error", "error": "rate_limited"}` while the cheap components
  still return. A session-start tool must remain callable when rate-limited; an unmetered
  recall bypass must not exist.
- The tool is a read: no rows are written except audit/usage. Side-effect semantics are
  inherited per component — `load_pinned` stays Hebbian-free (the "recall()-vs-list" rule);
  the `recall` component keeps its normal counter and reinforcement side effects,
  deliberately (bootstrap reads influence behavior and should reinforce).
- Exactly **one** billable `usage_stats` row per call (`mcp:get_agent_bootstrap`); delegated
  components add none because usage logging is handler-layer, not service-layer.
- Registration conformance: registry entry in `_build_registry()`; membership in
  `_TOOLS_WITHOUT_CONTEXT_ID` (context is optional).
- **No cross-component byte/token budget in v1** — each component is individually bounded. A
  `max_bundle_bytes` hint with proportional truncation interacts with the "never silent
  truncation" invariant and is deferred to its own design.

## Cross-repo follow-up (implementation checklist)

- `kagura-memory-python-sdk`: the `kagura-mcp` proxy forwards `/mcp` transparently (new tool
  auto-exposed, no change needed), but `client.py`/`cli.py` are per-tool explicit — file an
  SDK parity follow-up for `get_agent_bootstrap` when P0-3 lands.

## Sign-off checklist (maps to #1259)

- [x] Response envelope composing context guide + trusted recall + pinned + upcoming +
      state with per-component fail-soft status
- [x] Pure composition of existing primitives — no parallel retrieval path, no separate
      scoring; caps/bounds inherited from the composed primitives
- [x] Rate-limit exemption scoped to query-less calls only; query-carrying recall metered
      under normal accounting, degrading to a component-level `rate_limited` error
- [x] Identity rule: agent-bound key equality with uniform `agent_not_found`; owner/admin
      test bootstraps recorded with `on_behalf_of` + `principal_type` metadata
