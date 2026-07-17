# Design sign-off: Agent Registry & Context Bindings (RFC-0002 F1)

- **Status**: Implemented by [#1274](https://github.com/kagura-ai/memory-cloud/issues/1274) / [#1275](https://github.com/kagura-ai/memory-cloud/issues/1275); per-memory type/source filter enforcement (P1) shipped with [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299)
- **Issue**: [#1258](https://github.com/kagura-ai/memory-cloud/issues/1258) — gating item F1 of RFC-0002
  (Agent Memory & Context Control Plane; RFC text maintained locally, lands in
  `docs/rfc/0002-agent-memory-context-control-plane.md` when published)
- **Consumers**: maintainers and integrators of P0-1 (Agent Registry) and P0-2 (bindings + agent-scoped
  credentials), reviewers of the corresponding migrations

This document freezes the DDL, the migration plan, the backward-compatibility matrix, and
the erasure test plan for the Agent Registry. Implementation PRs must not deviate from the
normative sections without re-opening this sign-off.

## Scope and non-goals

In scope: `agents` and `agent_context_bindings` table definitions, the `api_keys.agent_id`
column, migration sequencing/safety, backward compatibility, erasure integration, and audit
requirements for registry/binding CRUD.

Out of scope (deliberately): the bootstrap contract (F2, #1259), the `memory_access_events`
audit table (F3, #1260), OTel correlation (F4, #1261), credential ops procedures (F5, #1262),
and any policy-bundle machinery (P1). Bindings are **advisory-scoping inside Kagura**; hard
allow/deny of agent *actions* belongs to an external gateway/runtime.

## Design invariants (normative)

1. **Agents are workspace-scoped resources, not principals.** An agent never authenticates by
   itself; its credential is an existing owner-provisioned, workspace-scoped member API key
   (`backend/src/api/routes/member_credentials.py`) that gains a nullable `agent_id` pointer. No
   `service_accounts` principal table, no per-agent OAuth clients, no parallel `agent_keys`
   scoping — the registry builds *on* workspace RBAC + `allowed_context_ids` + member keys and
   must never become a second authorization system.
2. **Bindings are purely subtractive.** The effective permission for an agent-bound request is
   *existing PermissionService decision ∩ binding*. Every request passes the established
   chokepoints first (`resolve_context_for_workspace_read`, `check_context_access`, and on MCP
   `_resolve_context[_for_read]`); only then is the binding filter applied. A binding can never
   grant access the underlying member row does not have. Enumeration surfaces
   (`list_contexts` / `get_accessible_contexts` and other membership-driven metadata reads) are
   intersected with the binding read set for enforce-mode agent credentials.
3. **The credential is the enforcement trigger.** Enforcement attaches to keys that carry
   `agent_id`; no existing key does. Requests without `agent_id` behave byte-for-byte as today.
4. **Deletion is fail-closed.** Deleting an agent kills its keys (CASCADE), never widens them
   (`SET NULL` rejected). Operational retirement is `agents.status`, not row deletion.

## DDL (normative)

All DDL follows the house Alembic/ORM conventions: plural snake_case tables, UUID PKs with
`server_default=gen_random_uuid()`, naive-UTC `DateTime`, named indexes declared in
`__table_args__`, CHECK constraints derived byte-identically from module-level Python tuples
(the `valid_delivery_mode` drift-pin pattern in `backend/src/models/memory.py`), and model
imports registered in both `models/__init__.py` and `alembic/env.py` so the
create_all-vs-alembic drift gate passes
(`backend/tests/integration/test_create_all_vs_alembic_drift.py`).

### `agents`

```sql
CREATE TABLE agents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- OTel gen_ai.agent.id
    workspace_id  UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,  -- tenancy is EXPLICIT
    name          VARCHAR(255) NOT NULL,                       -- OTel gen_ai.agent.name
    description   TEXT NULL,
    owner_user_id VARCHAR(255) NOT NULL,   -- OAuth sub; plain string per repo convention (cf. workspaces.owner_user_id)
    framework     VARCHAR(100) NULL,       -- free-form: 'claude-code', 'langgraph', ... (open set, no CHECK)
    environment   VARCHAR(100) NULL,       -- free-form, aligned with OTel deployment.environment.name
    version       VARCHAR(100) NULL,       -- agent build/prompt version, client-reported
    status        VARCHAR(20)  NOT NULL DEFAULT 'active',      -- CHECK from Python tuple
    enforcement_mode VARCHAR(20) NOT NULL DEFAULT 'enforce',   -- 'shadow' | 'enforce'
    last_seen_at  TIMESTAMP NULL,          -- write-throttled like api_keys.last_used_at (#947)
    created_at    TIMESTAMP NOT NULL DEFAULT now(),
    updated_at    TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT valid_agent_status CHECK (status IN ('active', 'suspended', 'retired')),
    CONSTRAINT valid_agent_enforcement CHECK (enforcement_mode IN ('shadow', 'enforce'))
);
CREATE UNIQUE INDEX uq_agents_workspace_name ON agents (workspace_id, name);
CREATE INDEX idx_agents_workspace ON agents (workspace_id);
```

Notes:

- `status` is the fail-closed kill switch: `suspended`/`retired` agents cause every key bound
  to them to be rejected at verify time (one row update beats revoking N keys).
- `enforcement_mode='shadow'` records binding violations as `would_deny` audit rows while the
  request proceeds under legacy semantics — the migration ramp for binding existing, in-use
  keys. Newly created agents default to `enforce`.
- Registry CRUD is owner/admin-gated via existing RBAC, following the `setup_resource` gate
  sequence (role check → validation → duplicate check → plan gate → quota,
  `backend/src/mcp_server/tools/resource.py`).

### `agent_context_bindings`

```sql
CREATE TABLE agent_context_bindings (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id)   ON DELETE CASCADE,
    context_id    UUID NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
    can_read      BOOLEAN NOT NULL DEFAULT true,
    write_policy  VARCHAR(20) NOT NULL DEFAULT 'deny',   -- 'deny' | 'direct' ('staged' reserved, P1)
    is_default    BOOLEAN NOT NULL DEFAULT false,        -- bootstrap default-binding marker
    allowed_memory_types  VARCHAR(50)[] NULL,   -- NULL = all types; [] = none
    allowed_source_types  VARCHAR(20)[] NULL,   -- values from the memories.source_type set
    created_by    VARCHAR(255) NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT now(),
    updated_at    TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT valid_binding_write_policy CHECK (write_policy IN ('deny', 'direct'))
);
CREATE UNIQUE INDEX uq_agent_ctx_binding ON agent_context_bindings (agent_id, context_id);
CREATE UNIQUE INDEX uq_agent_ctx_binding_default ON agent_context_bindings (agent_id) WHERE is_default;
CREATE INDEX idx_agent_ctx_binding_context ON agent_context_bindings (context_id);
```

Semantics (normative):

- The agent's **read set** is the rows with `can_read = true`; its **write set** is the rows
  with `write_policy = 'direct'`. Writes against a context whose binding says `deny` return
  the uniform `context_not_found` error (CWE-639 posture, same as today's deny paths).
- **NULL/[] array semantics are fixed**: `NULL` = unrestricted, `[]` = deny-all. We do not
  reproduce the role-dependent `allowed_context_ids` NULL asymmetry in a new table.
- **Per-memory type/source filtering is a P1 layer on top of the P0-2 context-level sets**
  (resolving the earlier normative-vs-DDL ambiguity: P0-2 was context-level only; the array
  columns were schema-reserved until [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299)
  shipped the enforcement). As of #1299, for **enforce-mode** agent credentials the
  **memory-read lanes** — `recall` candidates, `reference`, `forget`, `explore`
  seed/neighbors, declared-link refs, `load_pinned`, the upcoming time lane — additionally
  match each returned row against the binding's `allowed_memory_types` /
  `allowed_source_types` by the memory's own `type` / `source_type`: subtractive, composing
  with (never replacing) the context-level gate, at the service layer so REST and MCP behave
  identically for these operations by construction (#1291/#1292 lesson). Shadow-mode
  credentials are not filtered; row-filter violations land as `would_deny` aggregates in
  `memory_access_events` with `filter_kind='type_source'`. The write lane (`remember` /
  `update` / `patch`) stays governed by `write_policy` for whether the **mutation** may run;
  as of [#1301](https://github.com/kagura-ai/memory-cloud/issues/1301) the id-addressed
  update paths (`PATCH /memory/{id}`, MCP-reachable in-place update) additionally thread the
  target row's `type`/`source_type` through the row filter, so updating a read-denied row is
  a uniform 404 (the reference/forget doctrine — a no-op PATCH must not read back L3 content
  the read lanes would hide).
  - **Enumeration / aggregate surfaces (closed by
    [#1301](https://github.com/kagura-ai/memory-cloud/issues/1301)):** `GET /memory/list`,
    `GET /memory/access-patterns`, and `GET /memory/stats` (with its MCP mirror
    `get_context_info`) subtract denied rows via the SQL form of the same filter
    (`binding_memory_sql_predicate`) applied identically to page and count queries, so
    totals and grouped counts (`by_type`, type distribution) are not an existence oracle
    over denied types. MCP `get_cluster` filters member and representative rows through the
    shared row lever (`cluster.count` remains the stored whole-cluster size — a
    metadata-only aggregate). These surfaces sit outside the MAE operation vocabulary:
    shadow mode is a pure no-op there (no `would_deny` aggregates); the enforcement ramp
    observes would-deny volume through the `recall` / `load_pinned` lanes.
  - **Internal maintenance carve-out (#1301):** the upsert-by-`external_id` replacement
    delete calls `forget` with the per-memory filter skipped (context-level write gate still
    applies) — otherwise replacing a denied-type row would silently no-op and leave a live
    duplicate per `external_id`. The flag is service-internal and not reachable from any
    transport layer.
- **`is_default` is the single source of truth for the bootstrap default binding.** At most
  one binding per agent is default, guaranteed by the partial unique index
  `(agent_id) WHERE is_default`. `agents.default_context_id` was rejected as a second source
  of truth that can drift from binding rows.
- **`write_policy` ships as a two-value enum** with `'staged'` reserved for P1 (CHECK tuples
  are append-only by house convention, so adding the value later is a cheap migration).
- The service layer validates `contexts.workspace_id == agents.workspace_id` at binding
  create/update — a cross-workspace binding row would be inert under the subtractive rule,
  but it is dead weight and a confusing admin surface.

### `api_keys.agent_id`

```sql
ALTER TABLE api_keys ADD COLUMN agent_id UUID NULL
    REFERENCES agents(id) ON DELETE CASCADE;
-- agent binding and public binding are mutually exclusive, extending the existing
-- CHECK precedent (ck_api_keys_binding_workspace_exclusion, backend/src/models/auth.py):
ALTER TABLE api_keys ADD CONSTRAINT ck_api_keys_agent_public_exclusion
    CHECK (agent_id IS NULL OR bound_context_id IS NULL) NOT VALID;
ALTER TABLE api_keys VALIDATE CONSTRAINT ck_api_keys_agent_public_exclusion;
CREATE INDEX CONCURRENTLY idx_api_keys_agent ON api_keys (agent_id) WHERE agent_id IS NOT NULL;
```

- **`ON DELETE CASCADE`, not `SET NULL`, not `RESTRICT`.** `SET NULL` would silently widen a
  dead agent's keys back to full member scope — privilege escalation by deletion. `RESTRICT`
  is incompatible with the workspace-cascade hard-delete path: account erasure bulk-DELETEs
  sole-owner workspaces relying on DB cascades (`backend/src/services/account_erasure_service.py`),
  `api_keys.workspace_id` is already `ON DELETE CASCADE`, and programmatic revoke deliberately
  retains soft-revoked key rows for forensics — so agent-referencing key rows are guaranteed
  to linger, and under `RESTRICT` the agents cascade could abort a legally mandated erasure
  with an FK violation.
- **Key↔agent workspace consistency is enforced at mint time in the service layer**
  (`api_keys.workspace_id` MUST equal `agents.workspace_id`; PostgreSQL cannot express this
  cross-table invariant in a CHECK). The verify path re-asserts it defensively. An agent-bound
  key with NULL `workspace_id` (global key shape) is rejected at mint.

## Migration plan (normative)

**Revision naming.** The RFC sketch cited `e61_*` chaining from `e60_1228_read_attributions`.
The implementation landed as `e63_1274_agents`, `e64_1275_agent_bindings`, and
`e65_1275_api_keys_agent`. Follow-ups chain from the actual current head using the next free
`eNN_<issue>_<slug>` identifier (revision ids ≤ 32 chars, linear chain, symmetric downgrades).

**Two distinct migration classes**, shipped as separate revisions:

| Class | Content | Safety argument |
|---|---|---|
| 1. Pure additive | `CREATE TABLE agents`, `CREATE TABLE agent_context_bindings` + indexes | Blue-green safe by the house definition: no existing table is touched |
| 2. `api_keys` ALTERs | nullable `agent_id` FK; `ck_api_keys_agent_public_exclusion` CHECK added `NOT VALID` then `VALIDATE CONSTRAINT` as a separate statement; `CREATE INDEX CONCURRENTLY idx_api_keys_agent` | The hottest auth table never holds a long ACCESS EXCLUSIVE lock. Blue-green argument: old app versions always write `agent_id` NULL, which satisfies the new constraint |

Additional requirements:

- **Rerun safety**: `CREATE INDEX CONCURRENTLY` and `VALIDATE CONSTRAINT` run outside a
  transaction (Alembic autocommit block); a partial failure must be re-runnable. Use the
  established idempotent-DDL guards (`IF NOT EXISTS` where PostgreSQL supports it; a
  `DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL; END $$` wrapper for `ADD CONSTRAINT`,
  per the #655 hardening pattern).
- **ORM registration**: new models imported in `models/__init__.py` and `alembic/env.py`; both
  integration gates must pass (`backend/tests/integration/test_alembic_migrations.py`
  round-trip including `downgrade -1`, and the create_all/alembic drift diff).
- **Downgrades are symmetric**: drop index → drop constraint → drop column → drop tables, in
  reverse dependency order.
- No data backfill is required in any phase — all new columns are nullable or defaulted.

## Backward-compatibility matrix (normative)

| Caller / credential | Before | After (Phase A: schema only) | After (Phase B: enforcement live) |
|---|---|---|---|
| REST/MCP request, key without `agent_id` (every credential existing today) | current behavior | **byte-for-byte unchanged** | **byte-for-byte unchanged** |
| Key with `agent_id`, agent `enforce`, context **has** binding row | n/a | n/a | existing RBAC decision ∩ context-level binding (`can_read`, `write_policy`) ∩ per-memory type/source filter (`allowed_memory_types` / `allowed_source_types`, enforced per read-lane row as of #1299 — P1, split out of #1286) |
| Key with `agent_id`, agent `enforce`, context has **no** binding row | n/a | n/a | denied — uniform `context_not_found` (default-deny applies **only to newly bound agents**) |
| Key with `agent_id`, agent `shadow` | n/a | n/a | legacy semantics; violations recorded as `would_deny` audit rows |
| Key with `agent_id`, agent `suspended`/`retired` | n/a | n/a | rejected at verify time (fail-closed kill switch) |
| Enumeration surfaces (`list_contexts` / `get_accessible_contexts`) for enforce-mode agent keys | full membership view | unchanged | intersected with the binding read set |
| Share keys / public-bound keys / session keys | current behavior | unchanged | unchanged (`agent_id` and `bound_context_id` are mutually exclusive by CHECK) |
| Owner-provisioned mint without `agent_id` | works | works | works (Phase C strictness is per-deployment opt-in, default `false` indefinitely) |

No workspace-wide flag flips behavior for unbound clients, ever. A workspace-level enforcement
toggle was rejected: it would couple unrelated credentials' migrations and create a flag-day.

## Erasure integration test (required in the migration PR)

Scenario (validates CASCADE ordering against the account-erasure workspace hard-delete path):

1. Create user U owning workspace W; register agent A in W.
2. Create a service member M in W; owner-mint a member API key K for M with `agent_id = A`
   (mandatory expiry, per the #1165 flow).
3. Create context C in W with an `agent_context_bindings` row (A, C), `is_default = true`.
4. Soft-revoke a second agent-bound key K2 (revoked rows are retained for forensics — this is
   the row shape that would break `RESTRICT`).
5. Erase account U through `account_erasure_service`.
6. Assert: workspace W, agent A, both key rows (K, K2 — including the soft-revoked one), and
   the binding row are all gone; the erasure run completes without FK violations; no orphan
   rows remain in `agents`, `agent_context_bindings`, or `api_keys`.

## Data-boundary classification and pseudonymization

The PRs that create these tables MUST, in the same PR (the two-sided closure the
`llm_call_log` gap taught):

- add `agents` and `agent_context_bindings` to `data_boundary.OPERATIONAL_TABLES`
  (`backend/src/models/data_boundary.py`), and
- extend `account_erasure_service` so that `agents.owner_user_id` and
  `agent_context_bindings.created_by` are pseudonymized for erased subjects whose rows survive
  (e.g. agents in co-owned workspaces that are not hard-deleted).

## Audit requirements for registry/binding CRUD

- Agent create/update (including `status` and `enforcement_mode` transitions) and binding
  create/update/delete are security-relevant privilege changes and MUST write `audit_logs`
  rows via the existing security-mutation lane — recording actor, acting key prefix, and
  old→new values under the established hash convention, mirroring
  `member_api_key_provisioned` (`backend/src/auth/programmatic_workspace_auth.py`).
- **`enforce` → `shadow` is an audited privilege-widening event**: it silently widens every
  key bound to the agent back to full member scope (containment off, attribution on).
- Type/source filter **relaxation** (`[]` → `NULL`, adding types) is captured by the
  `agent_binding_updated` old→new changes dict (#1299). A first-class binding-level
  widening action (the #1294 per-transition pattern applied to filter relaxation) is
  deliberately deferred — extend this section when that governance lands.

## Sign-off checklist (maps to #1258)

- [x] `agents` + `agent_context_bindings` DDL — workspace-scoped registry; purely subtractive
      bindings; `is_default` with partial unique index
- [x] Migration plan — pure CREATE TABLE class vs `api_keys` ALTER class; `NOT VALID` +
      `VALIDATE CONSTRAINT`; `CREATE INDEX CONCURRENTLY`; revision naming updated to chain
      from the current e-series head
- [x] Backward-compat matrix — no `agent_id` ⇒ unchanged; default-deny only for newly bound
      agents; enumeration-surface intersection in enforce mode
- [x] Erasure integration test scenario fixed (CASCADE ordering vs workspace hard-delete)
- [x] Data-boundary classification + pseudonymization requirements assigned to the creating PRs
- [x] Registry/binding CRUD audit requirements via the security-mutation lane;
      `enforce`→`shadow` classified as privilege-widening
