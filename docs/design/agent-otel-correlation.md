# Design sign-off: OTel GenAI attribute mapping for agent correlation (RFC-0002 F4)

- **Status**: Signed off (gating design for P0-4 implementation)
- **Issue**: [#1261](https://github.com/kagura-ai/memory-cloud/issues/1261) — gating item F4 of RFC-0002
  (Agent Memory & Context Control Plane; RFC text maintained locally, lands in
  `docs/rfc/0002-agent-memory-context-control-plane.md` when published)
- **Consumers**: implementers of P0-4 (correlation parsing + identity precedence) and P0-5
  (`memory_access_events` correlation columns), operators joining Kagura audit rows against
  their own tracing backend, SDK maintainers
- **Depends on**: Agent Registry & Context Bindings (F1, #1258) for agent identity and
  agent-bound credentials; [`get_agent_bootstrap` contract](agent-bootstrap-contract.md)
  (F2, #1259) for the one surface where `agent_id`/`session_id` are explicit parameters

Kagura stores correlation identifiers (`agent_id`, `session_id`, `run_id`, `trace_id`,
`span_id`) on audit and usage rows and makes them **joinable against the operator's own
tracing backend**. It adopts W3C Trace Context plus the OpenTelemetry GenAI
semantic-conventions attribute names; it does not require, invent, or ship a proprietary
trace protocol. A bespoke `X-Kagura-Trace` scheme was rejected — it would lock consumers
into Kagura-specific plumbing and duplicate OTel.

## Scope and non-goals

**In scope (P0-4)**: the identifier vocabulary and its OTel attribute mapping; the transport
(headers/baggage, never tool parameters); the normative identity-precedence and
claim-verification rules; validation and server-side ID generation; the vendor-attribute gap
process for run identification.

**Non-goals**:

- **No server-side span emission in P0.** Kagura does not emit OTel spans; storing joinable
  IDs is the contract. P1 adds a *read-side export* of audit rows in GenAI-convention
  span/event format — a data-export surface, not Kagura becoming an inline tracing component.
  Upstream trace-context *acceptance* is P0 (this document).
- **No session or run tables in P0.** `session_id`/`run_id` are validated correlation columns
  only. When `agent_sessions` materializes in P1 it is lazily upserted (first-seen, keyed on
  `(agent_id, session_id)`) — a mandatory "open session" RPC was rejected because it would add
  a round trip to every agent turn loop and break stateless callers. Runs are never
  materialized in P0.
- **Correlation is observability, never authorization.** Headers and baggage are advisory
  data. Nothing in this design grants or denies access; authorization stays on the existing
  RBAC + `allowed_context_ids` + agent-binding chain.

## Identifier vocabulary and OTel mapping (normative)

Adopt the published OpenTelemetry GenAI semantic-conventions attribute names — do not invent
a closed trace format. The names below (`gen_ai.agent.id`, `gen_ai.agent.name`,
`gen_ai.conversation.id`) are the ones published in the OTel GenAI semantic-conventions
registry at adoption time (stability level: development). The authoritative registry is the
dedicated **`open-telemetry/semantic-conventions-genai`** repository — the `gen_ai.*`
namespace was split out of the main `open-telemetry/semantic-conventions` repo in May 2026,
and the main repo's remaining entries for these attributes are deprecated moved-pointers
("Moved to the OpenTelemetry GenAI semantic conventions repository"). The [gap-tracking
section](#vendor-attribute-gap-kaguraagentrunid) below covers renames and stabilization.

| Kagura identifier | Format | OTel attribute / standard | Notes |
|---|---|---|---|
| `agent_id` | UUID (Agent Registry PK) | `gen_ai.agent.id` | The agent display name maps to `gen_ai.agent.name` but is **not** duplicated onto events — join the registry |
| `session_id` | opaque, ≤128 chars, `[A-Za-z0-9._-]` | `gen_ai.conversation.id` (general-purpose alias: `session.id`) | One conversation / interactive session |
| `run_id` | opaque, ≤128 chars, `[A-Za-z0-9._-]` | **no stable GenAI key exists yet** — vendor attribute `kagura.agent.run.id` | Semconv gap; tracked below |
| `trace_id` | 32 lowercase hex | W3C `traceparent` trace-id | |
| `span_id` | 16 lowercase hex | W3C `traceparent` parent-id | |

Clients MUST NOT embed user identifiers, prompts, or other PII in `session_id`/`run_id`;
they are contractually opaque correlation tokens. This contract is what lets the audit table
store them verbatim — and because charset validation cannot actually stop a client from
embedding a name, the erasure lane retains the ability to pseudonymize these columns (the
append-only trigger's narrow carve-out, specified with the audit-table design, F3).

## Transport: IDs ride W3C Trace Context headers/baggage (normative)

Correlation rides on the HTTP headers `traceparent`, `tracestate`, and W3C `baggage` —
**never on per-tool parameters**. Baggage entries use the attribute keys from the table
above:

```text
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
baggage: gen_ai.agent.id=123e4567-e89b-12d3-a456-426614174000,
         gen_ai.conversation.id=sess-example-01,
         kagura.agent.run.id=run-example-01
```

(All values above are dummies for illustration.)

Headers are additive and ignored by servers/clients that do not understand them, so **no
existing client breaks and no tool schema changes** — critical because the MCP tool surface
is frozen under the strict-schema policy (#990): `additionalProperties: false` is stamped
centrally in `get_tool_definitions` (`backend/src/mcp_server/tools/_definitions.py`) and
enforced by `backend/tests/mcp_server/test_tool_schema_policy.py`
(`test_all_object_schemas_are_strict`). Rejected alternatives: MCP `_meta` fields (uneven
client support; would not cover REST) and per-tool `trace_id` params (schema churn across
~60 tools, breaking the frozen surface).

**Mechanism** (both surfaces, one pattern): the MCP transport already authenticates once per
request and stashes the pure API-key workspace scope in a per-request contextvar —
`authenticate_mcp_request` followed by `set_mcp_key_workspace_scope` in
`backend/src/mcp_server/transport.py`, with the `ContextVar` and setter defined in
`backend/src/mcp_server/tools/_helpers.py`. Correlation parsing adds a **sibling
contextvar** populated at that same point, read implicitly by the audit/usage writers at
service chokepoints — no parameter threading through handler signatures. REST gets the same
via middleware for `/api/v1/*`, as a sibling of the existing
`RequestLoggingMiddleware` (`backend/src/api/middleware/request_logger.py`, registered in
`backend/src/api/main.py`).

The **only** explicit parameters anywhere are `agent_id`/`session_id` on
`get_agent_bootstrap` ([bootstrap contract](agent-bootstrap-contract.md)), because there they
are functional inputs (which agent to bootstrap, which session to stamp on the bundle), not
just telemetry.

## Identity precedence and claim verification (normative)

The credential-derived identity is the only *authenticated* agent identity. Per the Agent
Registry design (F1, #1258), an agent's credential is an ordinary workspace-scoped member API
key that gains a nullable `agent_id` binding; verification surfaces it on the authenticated
principal. (Current tree: `VerifiedKey` in `backend/src/auth/api_keys.py` carries no
`agent_id` field yet — the NamedTuple was explicitly designed for additive attribution
shapes, and F1/P0-2 adds the field alongside the `api_keys.agent_id` column. The
owner-provisioned mint flow it extends is `backend/src/api/routes/member_credentials.py`.)

**Precedence order**:

> credential-bound `agent_id` (verified key) **>** explicit bootstrap arg **>** baggage claim

with the following hard rules:

1. **A claim never outranks a credential.** An explicit bootstrap `agent_id` or a baggage
   `gen_ai.agent.id` claim that disagrees with a credential-bound `agent_id` MUST be either
   rejected (bootstrap: uniform `agent_not_found` — nonexistent and not-yours are
   indistinguishable) or stored only as unverified metadata
   (`event_metadata.unverified_agent_claim`, as a keyed hash — see below) with the audit
   row's `agent_id` taken from the credential. A claim is **never precedence-resolved in
   favor of the claim** against the credential.
2. **Baggage claims are verified before being trusted in audit rows.** This verified-claim
   path applies **only when the authenticated credential carries no `api_keys.agent_id`
   binding**; when the credential is agent-bound, Rule 1 controls unconditionally and a
   disagreeing claim is never written to `agent_id`, regardless of any same-member binding of
   the claimed agent. For an agent-unbound credential, the verification predicate is precise:
   a baggage `gen_ai.agent.id` claim verifies **iff** the claimed agent is bound, via
   `api_keys.agent_id`, to the same member row as the authenticated credential. A verified
   claim may populate the audit row's `agent_id` column; because the credential itself is
   bound to no agent, such rows are stamped `policy_decision='unbound'` so the
   attribution-without-containment state is explicit in every row, never implied.
3. **An unverified claim never reaches the `agent_id` column.** The request proceeds
   unchanged (correlation is advisory; rejecting requests on bad headers would let a
   misconfigured proxy take down working clients), and the claimed value is recorded only as
   `event_metadata.unverified_agent_claim`, stored as a keyed hash — HMAC-SHA256 with the
   dedicated audit key (the `audit_hmac_key` setting,
   `backend/src/config/settings.py`) — never verbatim. The rejected alternative here was
   trusting claims **without** Rule 2's same-member verification and its explicit
   `policy_decision='unbound'` stamp: writing unchecked claims to `agent_id` would let any
   caller forge an arbitrary agent's audit trail. Rule 2's verified path is the answer to
   that threat, not an instance of it — the same-member check confines attribution to agents
   demonstrably operated by the same authenticated service member, and the `unbound` stamp
   keeps such rows distinguishable from credential-verified attribution.
4. **Only `get_agent_bootstrap`'s explicit `agent_id` hard-fails** on mismatch (uniform
   `agent_not_found`), because there it is a functional input, not telemetry.
5. **Credentials not bound to any agent**: if an explicit `agent_id` disagrees with baggage,
   the explicit value wins and the event records `correlation_conflict: true` in metadata.

## Vendor-attribute gap: `kagura.agent.run.id`

The GenAI semantic conventions define agent and conversation identity but, at adoption time,
**no stable attribute for a run/execution identifier**. Kagura therefore carries `run_id` as
the vendor attribute `kagura.agent.run.id` (vendor-namespaced per OTel attribute-naming
guidance), with an explicit migration obligation:

- **Track upstream.** The gap is tracked against the dedicated
  `open-telemetry/semantic-conventions-genai` repository — since the May 2026 split, GenAI
  attribute evolution no longer flows through the main `open-telemetry/semantic-conventions`
  artifact, whose `gen_ai.*` entries are deprecated moved-pointers. Watch that repo's
  agent-run / workflow identification proposals: at the time of writing it defines
  `gen_ai.workflow.name` but still no run/execution *identifier* attribute, so the gap
  stands. Ownership: revisit at each Kagura release cycle and whenever the pinned GenAI
  semconv registry version is bumped; the tracking issue for this gap is filed as part of
  P0-4 and linked from the model docstring of the audit table.
- **Migrate, don't fork.** When a standard key lands, Kagura adopts it additively: new rows
  and exports carry the standard attribute; `kagura.agent.run.id` continues to be accepted on
  ingest for a deprecation window; the stored column (`run_id`) is name-agnostic, so
  migration is confined to the header-parsing and P1 export layers — no schema change.
- The same watch covers potential renames or stabilization of `gen_ai.agent.id` /
  `gen_ai.agent.name` / `gen_ai.conversation.id` (currently development-stability): stored
  columns are Kagura-named, mappings live in one place (the parsing/export layer), so an
  upstream rename is a constant change, not a migration.
- The dedicated registry also defines `gen_ai.memory.*` attributes (`gen_ai.memory.record.id`,
  `gen_ai.memory.store.id`) that are directly relevant to the P1 read-side export of audit
  rows in GenAI-convention format — the P1 export design should evaluate mapping onto them
  before minting any further vendor keys.

## Validation and server-side generation (normative)

- `trace_id`/`span_id` are accepted only in W3C form (32 / 16 lowercase hex). If no
  `traceparent` arrives, the server **generates** `trace_id`/`span_id` per request so audit
  rows are always correlatable.
- `session_id`/`run_id` are recorded verbatim after charset (`[A-Za-z0-9._-]`) and length
  (≤128) validation; invalid values are dropped (advisory data — never a request failure),
  with a structured warning.
- P0 records `session_id`/`run_id` as validated correlation columns only; no session row is
  created (see non-goals).
- Composition semantics: all audit rows emitted within one request — e.g. a `bootstrap`
  parent row and its delegated `recall` component row — share the request's trace/span
  context.

## Privacy rules for correlation data (normative)

- Correlation tokens are identifiers, never content: audit/usage rows store IDs, outcomes,
  latency, and keyed hashes — never raw prompts, queries, memory content, secrets, emails,
  or IPs/user-agents.
- `event_metadata.unverified_agent_claim` is always a keyed hash (HMAC-SHA256,
  dedicated `audit_hmac_key`), never the verbatim claim.
- Because `session_id`/`run_id` are client-controlled despite the opacity contract, the
  erasure lane can pseudonymize them (the F3 append-only carve-out); this document's contract
  is what makes verbatim storage acceptable in the first place.

## Repo reality notes (descriptive)

- The RFC's DDL sketches reference migration ids `e61_*` chaining from head
  `e60_1228_read_attributions`; both are stale against the current tree. The repo's alembic
  head is `e62_1245_assign_mem_idx`
  (`backend/alembic/versions/e62_1245_assign_mem_idx.py`), and `e61_`/`e62_` prefixes are
  already taken. New migrations chain from the current head at implementation time; nothing
  in this design depends on a specific revision id.
- There is no OTel/`traceparent`/baggage handling anywhere in `backend/src` today; P0-4
  introduces it at the two seams named above (MCP transport auth point, REST middleware).

## Sign-off checklist (maps to #1261)

- [x] `agent_id` / `session_id` / `run_id` / `trace_id` / `span_id` mapped to published
      `gen_ai.*` attributes (`gen_ai.agent.id`, `gen_ai.agent.name`,
      `gen_ai.conversation.id`) and W3C `traceparent` fields — adopted, not invented; no
      closed trace format
- [x] Identity precedence (normative): credential-bound `agent_id` (verified key) > explicit
      bootstrap arg > baggage; a claim disagreeing with a credential-bound `agent_id` is
      rejected (uniform `agent_not_found`) or stored only as unverified metadata
      (`event_metadata.unverified_agent_claim`, keyed hash) — never precedence-resolved in
      favor of the claim
- [x] Vendor-attribute gap for run identification (`kagura.agent.run.id`) tracked against
      upstream semconv evolution, with an additive migration path when a standard key lands
- [x] IDs ride W3C Trace Context headers/baggage (`traceparent`, `tracestate`, `baggage`) —
      no per-tool parameter changes for existing clients; the frozen
      `additionalProperties: false` MCP surface is untouched
