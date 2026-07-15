# Design sign-off: `memory_access_events` schema + deny-logging review (RFC-0002 F3)

- **Status**: Implemented by [#1278](https://github.com/kagura-ai/memory-cloud/issues/1278) / PR #1284; remaining operation emission and deny capture are tracked by [#1286](https://github.com/kagura-ai/memory-cloud/issues/1286)
- **Issue**: [#1260](https://github.com/kagura-ai/memory-cloud/issues/1260) — gating item F3 of RFC-0002
  (Agent Memory & Context Control Plane; RFC text maintained locally and currently
  unpublished — it would land at `docs/rfc/0002-agent-memory-context-control-plane.md`;
  this document is self-contained and does not require it)
- **Consumers**: maintainers of P0-5 (`memory_access_events` migration + audit writer),
  the CSO review record for the deny-logging layer, ops (retention escalation, F7)
- **Depends on**: the Agent Registry & Context Bindings sign-off
  (`docs/design/agent-registry-and-bindings.md`, F1,
  [#1258](https://github.com/kagura-ai/memory-cloud/issues/1258), implemented by #1274/#1275)
  for the agent/binding vocabulary; the session/run/trace correlation design
  (F4, [#1261](https://github.com/kagura-ai/memory-cloud/issues/1261), implemented by
  [#1277](https://github.com/kagura-ai/memory-cloud/issues/1277)) for the semantics of the
  correlation columns this table stores

`MemoryAccessEvent` (`memory_access_events`) is the append-only audit row for the eight
memory operations performed under verified agent identity: **recall / reference / remember /
update / forget / load_pinned / bootstrap / feedback**. Rows store identifiers, outcome,
latency, policy decision, and keyed (HMAC) hashes — **never** raw prompts, retrieved
content, secrets, or PII. This document restates every RFC-0002 decision the schema depends
on (D20–D25, D29, D34) so it is self-contained, records the CSO review of the deny-logging
layer, and settles the one question RFC-0002 deferred to F3 (the deny-capture layer).

The v0.49.0 implementation emits bootstrap, load-pinned, feedback, recall, reference, and
remember events. The schema already reserves all eight operations; `update`/`forget`
emission plus denied/`would_deny` persistence remain in #1286.

## Scope and non-goals

**In scope**: the canonical DDL; the audited population; emission points; privacy rules;
append-only enforcement tier and its erasure carve-out; data-boundary classification and the
pseudonymize/scrub lane; the deny-logging layer (existence-oracle review, capture point,
attribution rule); writer failure posture; retention posture.

**Non-goals**:

- **Not a quota/billing lane.** `usage_stats` row-count consumers (quota, billing,
  analytics) never see this table and never need exclusion filters — the same structural
  invisibility rationale as `context_read_attributions`
  (`ContextReadAttribution`, `backend/src/models/auth.py`).
- **Not tamper-evident in P0.** The secret store's per-workspace HMAC hash chain
  (`SecretAccessLog`, `backend/src/models/secrets.py`) is the designated P1 upgrade if
  tamper evidence is demanded; its advisory-lock serialization is unacceptable on a path
  that fires on every `recall` and every `load_pinned` turn.
- **Not all-traffic logging.** Auditing unbound (non-agent) traffic — including
  all-principal deny logging for probe detection — is a P1 extension, setting-gated,
  **default off**, with its own CSO/privacy review (see the audited population below).
- **Not a ranking input.** Contrast `retrieval_feedback`
  (`backend/src/models/retrieval_feedback.py`), which stores truncated query text by design
  because the learning lane needs it; the audit lane must never feed ranking.

## Audited operations and emission points (normative)

**Events are emitted at service-layer chokepoints, not handlers** (RFC-0002 D20), so MCP
and REST are covered identically — the same reason `allowed_context_ids` and workspace
confinement apply uniformly to both surfaces today (shared `PermissionService`;
MCP resolves through `_resolve_context_for_read` in
`backend/src/mcp_server/tools/_helpers.py`, REST through
`PermissionService.resolve_context_for_workspace_read` / `check_context_access` in
`backend/src/services/permission_service.py`). The operation enum is closed and
append-only (extension = additive migration), following the append-only-tuple convention of
`memories.source_type` (`backend/src/models/memory.py`).

| `operation` | Emitting chokepoint (verified in-tree symbol) |
|---|---|
| `recall` | the recall hydration path — the same code that applies the trusted-tier filter (`MemoryService`, `backend/src/services/memory_service.py`) |
| `reference` | `MemoryService.reference` (the `reference_count` adoption-bump path) |
| `remember` | `MemoryService.remember` |
| `update` | `MemoryService.update_memory` / `_update_in_place` |
| `forget` | `MemoryService.forget` (soft-delete path) |
| `load_pinned` | `MemoryService.load_pinned` |
| `bootstrap` | `AgentBootstrapService` (P0-3, implemented by #1276; see `docs/design/agent-bootstrap-contract.md` — F2, [#1259](https://github.com/kagura-ai/memory-cloud/issues/1259)) |
| `feedback` | `FeedbackService.record_feedback` (`backend/src/services/feedback_service.py`) — the same service whose non-agent-callable `record_host_feedback` seam the gateway integration uses |

Composition semantics: because instrumentation lives in the chokepoints,
`get_agent_bootstrap`'s delegated `recall` emits its own `recall` row exactly as a direct
call would, plus one parent `bootstrap` row; all rows share the request's trace/span
context. Suppressing component events would create an unaudited retrieval path — precisely
what the bootstrap contract's pure-composition rule forbids. Non-audited component
delegations (`upcoming`, `state` — not in the P0 enum) have their outcomes summarized in
the `bootstrap` row's metadata.

## Audited population (fixed input 1, normative)

**P0 writes `memory_access_events` only for requests carrying verified agent identity**
(RFC-0002 D34). A request is in the audited population iff its agent identity is verified —
a credential-bound `agent_id` (from the verified API key) or a baggage claim verified
against the same member row — and this includes shadow-mode `would_deny` rows.

Auditing unbound traffic — including all-principal deny logging for probe detection — is a
**P1 extension gated behind a deployment setting, default off, with its own CSO/privacy
review**. Flipping it changes the table's volume class, its privacy/erasure exposure (all
human traffic vs agent traffic only), and adds a hot-path INSERT to every legacy client's
recall. The backward-compatibility contract of RFC-0002 (no behavior or performance change
for requests without `agent_id`) requires that unbound legacy traffic gain no hot-path
write in P0.

Rejected alternatives: all-traffic auditing from day one (an unreviewed privacy expansion
plus a performance change to today's clients); leaving the population phase-ambiguous (an
implementer could not determine the table's volume class, erasure exposure, or whether
probe detection exists at all).

## Canonical table

DDL as specified by RFC-0002 (canonical; the migration must derive CHECK constraints
byte-identically from module-level Python tuples per the house drift-pin convention):

```sql
CREATE TABLE memory_access_events (
    id                 BIGSERIAL PRIMARY KEY,        -- house pattern for append-only logs
                                                     -- (SecretAccessLog, backend/src/models/secrets.py);
                                                     -- doubles as keyset cursor
                                                     -- (backend/src/services/resource_events.py)
    occurred_at        TIMESTAMP NOT NULL DEFAULT now(),  -- naive UTC (repo convention)
    -- identifiers only; NO foreign keys (rows outlive agents/contexts/keys)
    workspace_id       UUID NOT NULL,
    context_id         UUID NULL,                    -- NULL for cross-context recalls; attributed ids in event_metadata
    user_id            VARCHAR(255) NOT NULL,        -- OAuth sub, non-FK convention; pseudonymized on erasure
    principal_type     VARCHAR(16)  NOT NULL,        -- duck-typing precedent backend/src/auth/programmatic_workspace_auth.py:67-79
    api_key_prefix     VARCHAR(16)  NULL,            -- credential attribution without secrets (auth.py key_prefix)
    agent_id           UUID NULL,                    -- verified only (D18); non-NULL on every P0 row (D34);
                                                     -- NULL reserved for the P1 unbound-traffic extension
    session_id         VARCHAR(128) NULL,            -- OTel gen_ai.conversation.id
    run_id             VARCHAR(128) NULL,            -- kagura.agent.run.id
    trace_id           VARCHAR(32)  NULL,            -- W3C trace-context hex; adopt OTel, do not invent
    span_id            VARCHAR(16)  NULL,
    surface            VARCHAR(10)  NOT NULL,        -- 'mcp' | 'rest'
    operation          VARCHAR(20)  NOT NULL,        -- CHECK from Python tuple, byte-identical in migration
                                                     -- (llm_call_log.py:105-127 pattern; drift-gate enforced)
    outcome            VARCHAR(10)  NOT NULL,        -- 'partial' = degraded bootstrap (Decision D15)
    policy_decision    VARCHAR(20)  NULL,            -- binding evaluation result; 'would_deny' = shadow mode
    policy_revision_id UUID NULL,                    -- P1 pointer; NULL in P0
    memory_id          UUID NULL,                    -- single-target ops (reference/remember/update/forget)
    result_count       INTEGER NULL,                 -- set-returning ops (recall/load_pinned/bootstrap)
    latency_ms         INTEGER NULL,
    query_hash         VARCHAR(64) NULL,             -- HMAC-SHA256 of query text (dedicated key, see Privacy rules);
                                                     -- raw query NEVER stored
    event_metadata     JSONB NULL,
    CONSTRAINT valid_mae_operation CHECK (operation IN
        ('recall','reference','remember','update','forget','load_pinned','bootstrap','feedback')),
    CONSTRAINT valid_mae_outcome   CHECK (outcome IN ('success','denied','error','partial')),
    CONSTRAINT valid_mae_surface   CHECK (surface IN ('mcp','rest')),
    CONSTRAINT valid_mae_principal CHECK (principal_type IN ('api_key','oauth','session')),
    CONSTRAINT valid_mae_policy    CHECK (policy_decision IS NULL OR policy_decision IN
        ('allowed','binding_denied','rbac_denied','would_deny','unbound')),
    CONSTRAINT mae_metadata_size   CHECK (octet_length(event_metadata::text) <= 4096)
        -- canonical PII-sensitive JSONB cap (llm_call_log.py:134-140, 272-276)
);
CREATE INDEX idx_mae_occurred           ON memory_access_events (occurred_at);
CREATE INDEX idx_mae_workspace_occurred ON memory_access_events (workspace_id, occurred_at);  -- mirrors idx_llm_call_log_workspace_period
CREATE INDEX idx_mae_agent_occurred     ON memory_access_events (agent_id, occurred_at)
    WHERE agent_id IS NOT NULL;          -- partial: agent_id is non-NULL on every P0 row (D34); the partial form
                                         -- anticipates the P1 unbound-traffic extension, whose rows are agent-less
-- + BEFORE UPDATE OR DELETE trigger raising, per e50_1128 precedent
--   (UPDATE permitted only on user_id, session_id, run_id, event_metadata — the erasure carve-out, D22)
```

Schema notes:

- **No FKs to `contexts` / `workspaces` / `agents`; the trail survives entity deletion**
  (RFC-0002 D21). This is the audit-grade fork between the two in-repo precedents:
  `SecretAccessLog` (no-FK, survives — `backend/src/models/secrets.py`) vs
  `ContextReadAttribution` (CASCADE, telemetry-grade — `backend/src/models/auth.py`). An
  access-audit row for a deleted context is exactly the row an investigation needs.
  Consequence: the table must join the erasure story explicitly (below) instead of relying
  on cascades — avoiding the documented `llm_call_log` gap (plaintext `user_id`, no FK, no
  erasure step).
- **`policy_decision` semantics**: `allowed` / `binding_denied` / `rbac_denied` record the
  binding evaluation for credential-bound agent requests; `would_deny` is shadow mode;
  `unbound` is stamped when the row's agent identity comes from a verified baggage claim on
  a credential not itself bound to that agent — binding evaluation was skipped, and the
  attribution-without-containment state is explicit in the row, never implied. NULL is
  reserved for rows where binding evaluation is not applicable at all (the non-agent rows
  of the P1 unbound-traffic extension).
- For recall, up to 32 result `memory_ids` go in `event_metadata` under the 4 KB cap;
  identifiers are permitted payload, content is not.
- **Migration reality**: RFC-0002's sketch names an older head. F1/P0-2 landed as
  `e63_1274_agents` → `e64_1275_agent_bindings` → `e65_1275_api_keys_agent`; P0-5 landed as
  `e66_1278_memory_access_events`, followed by the `e67_1281_agent_key_workspace` invariant.
  The P0-5 migration is additive-only (blue-green safe), has a symmetric downgrade, and
  the ORM model is imported in both `models/__init__.py` and `alembic/env.py` so it passes
  both integration gates (`backend/tests/integration/test_alembic_migrations.py` round-trip
  and `backend/tests/integration/test_create_all_vs_alembic_drift.py`).

## Privacy rules (normative)

- Events MUST store only: identifiers (UUIDs, key prefixes, opaque correlation tokens),
  operation, outcome, latency, policy decision, and keyed hashes. Events MUST NEVER store
  raw prompts, recall queries, memory summaries/content/details, secret material, emails,
  or IPs/user-agents (volume + minimization; the low-volume `audit_logs` security lane
  keeps IP/UA for its actions — `AuditLog`, `backend/src/models/auth.py`).
- `query_hash` uses **HMAC-SHA256 with a dedicated key** — the `audit_hmac_key` setting
  (`backend/src/config/settings.py`), introduced exactly for keyed hashing of audit
  identifiers with an independent-rotation rationale — never a bare salted hash. Same-query
  correlation across rows works, while recall queries (short, low-entropy natural language)
  stay resistant to offline dictionary attack without the HMAC key; a per-deployment salt
  alone would not survive a DB-plus-settings leak.
- The same keyed hash is applied to `event_metadata.unverified_agent_claim` (a baggage
  agent claim that failed verification is never stored verbatim, and never reaches the
  `agent_id` column).
- Hash columns are 64-hex-char `VARCHAR(64)` (SHA-256 / HMAC-SHA256), the `audit_logs`
  convention.
- `session_id` / `run_id` are contractually opaque correlation tokens (clients MUST NOT
  embed user identifiers, prompts, or other PII); they are stored verbatim after
  charset/length validation — and precisely because a contract is not a control, they are
  inside the erasure carve-out below.

## Append-only enforcement + erasure carve-out (normative)

**DB-trigger append-only with a narrow pseudonymization carve-out; no HMAC chain in P0**
(RFC-0002 D22). A `BEFORE UPDATE OR DELETE` trigger — precedent:
`secret_access_log_append_only` / `secret_access_log_no_mutate()` in
`backend/alembic/versions/e50_1128_secret_store.py`, whose stated rationale is that
application discipline alone is not trusted — blocks **all DELETEs** and blocks **any
UPDATE that changes a column other than `user_id`, `session_id`, `run_id`, or
`event_metadata`**. The e50 precedent also adds a `BEFORE TRUNCATE` statement trigger;
P0-5 mirrors that.

The carve-out is exactly those four columns, and exists solely so GDPR/APPI erasure can
pseudonymize and scrub:

- `user_id` is an OAuth sub (personal data) stored without FK;
- `session_id` / `run_id` are stored verbatim on the strength of a contractual MUST NOT
  (charset validation does not stop a client embedding a person's name);
- `event_metadata.unverified_agent_claim` originates from client-controlled input even
  though it is stored as a keyed hash.

Rejected alternatives: app-discipline only (the weakest tier; this is an audit lane, not a
feedback lane); the full secret-store HMAC chain with per-workspace anti-fork (its
advisory-lock serialization is unacceptable on a path that fires on every `recall` and
every `load_pinned` turn, and memory access does not carry secret-fetch threat levels —
the chain remains the designated P1 upgrade).

## Data-boundary classification + erasure lane (normative)

**Erasure = pseudonymize-and-keep, wired in from day one, for every new table**
(RFC-0002 D23):

- `memory_access_events` — and the sibling P0 tables `agents` and
  `agent_context_bindings` — are classified in `OPERATIONAL_TABLES` in
  `backend/src/models/data_boundary.py` **in the same PRs that create them**. This is
  CI-enforced: unclassified tables fail `backend/tests/test_derived_layer_boundary.py`.
- Account erasure (`backend/src/services/account_erasure_service.py`) adds, for the erased
  subject:
  - a `_pseudonymize_field` pass over `memory_access_events.user_id` (salted SHA-256 with
    the `audit_pseudo_salt` setting, `backend/src/config/settings.py` — same lane as
    `_pseudonymize_audit_logs`);
  - a pseudonymizing rewrite of `session_id` / `run_id` (salted-hash rewrite, matching
    `_pseudonymize_field`);
  - an `event_metadata` scrub for rows belonging to the erased subject — the "correlation
    tokens are opaque" contract is not trusted to keep PII out of client-controlled
    columns (trusting the contract there is precisely the `llm_call_log` lesson).
- The same two-sided closure covers the sibling tables: `agents.owner_user_id` and
  `agent_context_bindings.created_by` are OAuth subs with no FK and are pseudonymized via
  the same `_pseudonymize_field` lane; `agent_sessions` receives identical treatment when
  it materializes in P1.

Rejected alternative: hard-delete on erasure like `usage_stats`
(`account_erasure_service.py` deletes those rows outright) — correct for billing rows keyed
to a user, wrong for an access-audit trail whose remaining value is non-personal.

## Deny-logging layer — CSO review (existence-oracle concern)

**Concern under review.** A table of denied accesses is itself a reconnaissance surface if
mishandled: (a) if a deny row were attributed to the *probed* workspace, that workspace's
audit readers would learn about cross-tenant probes naming their identifiers — and,
symmetrically, a prober could confirm a target's existence by observing which of its
requests produce rows readable somewhere; (b) if the deny response itself were enriched,
the uniform-404 posture (CWE-639) would break; (c) if requested identifiers were stored in
authoritative columns, a NOT NULL `workspace_id` could not be satisfied for probes of
nonexistent targets without fabricating or leaking target data.

The review was conducted against two **fixed inputs** (both normative, from RFC-0002 D34
and the deny-attribution rule):

1. **P0 audited population = requests carrying verified agent identity only.**
   Unbound-traffic auditing (all-principal deny logging, the population that contains
   today's dominant cross-tenant probe traffic) is a **P1 extension, setting-gated,
   default off, separately CSO/privacy-reviewed**. P0 deny logging therefore observes
   exactly the population whose identity is already authenticated — it adds detection for
   that slice and changes nothing for anyone else.
2. **`workspace_id` on denied events is always the CALLER's credential workspace scope**
   (the key's workspace for workspace-scoped keys, the current workspace for session
   principals), **never the target's**. Requested context/workspace identifiers go only
   into `event_metadata` as unverified claims. Consequences: deny rows never surface in a
   probed workspace's audit reads (no cross-tenant existence oracle); probes of
   nonexistent contexts still satisfy the NOT NULL column; and the row's authoritative
   columns contain only caller-derived, credential-verified data.

**Review findings (accepted posture):**

- **External uniformity is preserved.** Denied accesses are logged internally with the real
  deny reason while the external response stays uniform (`context_not_found` /
  uniform 403/404 — RFC-0002 D29), mirroring the existing log-only private-reason pattern
  in `PermissionService` (`backend/src/services/permission_service.py`, CWE-639 posture).
  Audit rows are never returned to callers on any request path.
- **A deny row records** `outcome='denied'`, the true reason in `policy_decision`
  (`binding_denied` vs `rbac_denied`) or metadata, and the *requested* identifiers only in
  `event_metadata` as unverified claims — which is where probe attempts become visible to
  the workspace's own operators.
- **The audit read surface (P1 fleet dashboard) MUST be workspace-scoped** — filtered by
  the caller-attributed `workspace_id` column — so the table never becomes a cross-tenant
  existence oracle in the read direction either. This is a standing requirement on any
  future read/export surface over this table, including the P1 OTel export.
- **Deny-capture layer (deferred question, settled here): service-layer post-resolve
  capture in P0.** RFC-0002 left "MCP dispatch pre-resolve vs service post-resolve" to F3.
  Decision: capture denies at the same service-layer chokepoints as success events.
  Rationale: (i) chokepoint emission is already the mechanism that makes audit
  surface-invariant across MCP and REST — a dispatch-level capture would be MCP-only and
  need a parallel REST twin, two capture points to keep honest; (ii) the main advantage of
  dispatch-level capture is catching malformed-ID probes, but in P0 the audited population
  is verified-agent traffic, where a malformed identifier is an `invalid_arguments`
  client bug, not a meaningful probe signal — the probe population that motivates
  pre-resolve capture is unbound traffic, which is exactly the P1 extension; (iii)
  post-resolve capture has the caller's verified scope in hand, so fixed input 2 is
  trivially satisfiable. The dispatch-level option is re-examined as part of the P1
  unbound-traffic extension's own CSO review.
- **Residual (stated plainly):** audit is detection, not new prevention; a workspace owner
  remains all-powerful inside their own workspace; and P0 deny logging does not see probes
  from unbound principals — that visibility arrives only with the P1 extension.

## Writer posture (normative)

**Hot-path-safe and fail-open in P0** (RFC-0002 D24): the writer runs on an independent
session, swallows failures with a structured warning, and logs `error_type` — never
`str(exc)` (the credential-leak guard established in
`backend/src/services/llm_call_log_writer.py`; same posture as `log_usage` in
`backend/src/utils/usage_logger.py`, where logging must never break the main flow).
Validation errors always raise. Rationale: P0 policy decisions are advisory, so a dropped
event degrades observability, not security. Rejected alternative: synchronous fail-closed
writes (turns audit-DB latency into recall latency). When P1 PolicyRevision lands,
policy-bearing bootstrap/`load_pinned` responses upgrade to synchronous
audit-before-return; whether `denied` events must also be written synchronously is decided
with the P1 policy design.

## Retention and quota invisibility

- Separate table precisely so `usage_stats` row-count consumers never need exclusion
  filters (the `context_read_attributions` rationale).
- v1 ships with unlimited retention, no partitioning, and an explicit escalation trigger
  documented in the model docstring (the `llm_call_log` pattern,
  `backend/src/models/llm_call_log.py`): **100M rows or 12 months, whichever first**.
  Under the P0 population this is ≥1 row per recall plus per `load_pinned` per
  **agent-bound** turn, so volume scales with agent adoption rather than total traffic;
  the P1 unbound-traffic extension MUST re-open this sizing (F7) before it can flip on.
- Monthly range partitioning on `occurred_at` is the pre-designated, additive escalation
  response; partitioning-vs-TTL needs production data (F7 ops issue).
- **No sampling in P0**: an audit lane that samples is not an audit lane; revisit only at
  the escalation trigger.

## Relation to existing event lanes

| Lane (verified in-tree) | Role | Why `memory_access_events` does not replace it |
|---|---|---|
| `usage_stats` (`backend/src/models/auth.py`) | per-request quota/billing | billing rows must survive as billing; access events are audit-shaped and quota-invisible |
| `audit_logs` (`backend/src/models/auth.py`) | low-volume security mutations | different volume class and writer discipline; stays the security-mutation lane (agent/binding CRUD writes go here, not to this table) |
| `llm_call_log` (`backend/src/models/llm_call_log.py`) | LLM cost ledger | records provider spend, not memory access; different consumers |
| `retrieval_feedback` (`backend/src/models/retrieval_feedback.py`) | learning signal (DERIVED_MOAT lane, `data_boundary.py`) | stores query text by design for offline analysis; feeds ranking — audit must never feed ranking |
| `context_read_attributions` (`backend/src/models/auth.py`) | cross-context billing attribution | telemetry-grade CASCADE semantics, deliberately opposite to this table's no-FK posture |
| `secret_access_log` (`backend/src/models/secrets.py`) | tamper-evident secret-fetch audit | stronger regime (HMAC hash chain) justified by its threat model; remains separate |

## Sign-off checklist (maps to #1260)

- [x] Append-only audit for recall / reference / remember / update / forget / load_pinned /
      bootstrap / feedback — rows store identifiers, outcome, latency, policy decision, and
      keyed (HMAC) hashes; never raw prompts, retrieved content, secrets, or PII (canonical
      DDL above; privacy rules normative; enforcement via the `e50_1128` BEFORE
      UPDATE OR DELETE trigger precedent)
- [x] Data-boundary classification + erasure lane: `OPERATIONAL_TABLES` classification in
      the creating PRs (CI-enforced by `test_derived_layer_boundary.py`); append-only
      trigger carve-out limited to `(user_id, session_id, run_id, event_metadata)`;
      pseudonymize/scrub pass for erased subjects via the existing
      `account_erasure_service` `_pseudonymize_field` lane
- [x] Deny-logging layer: CSO review of the existence-oracle concern completed against the
      two fixed inputs — (1) P0 audited population = requests carrying verified agent
      identity only, with unbound-traffic auditing a P1 setting-gated, default-off,
      separately reviewed extension; (2) `workspace_id` on denied events is always the
      caller's credential workspace scope, never the target's, with requested identifiers
      recorded only in `event_metadata` as unverified claims — and the deny-capture layer
      settled as service-layer post-resolve in P0
