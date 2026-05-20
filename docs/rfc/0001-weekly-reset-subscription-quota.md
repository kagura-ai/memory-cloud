# RFC-0001: Weekly-reset subscription quota with LiteLLM-backed budget enforcement

- **Issue**: [#693](https://github.com/kagura-ai/memory-cloud/issues/693)
- **Status**: Draft (Phase 3 design)
- **Consumers**: `kagura-memory-ai-worker` (Phase 2/3)
- **Last updated**: 2026-05-20

## Summary

Design a subscription quota model where each customer workspace receives a flat-rate tier with a **weekly LLM budget (USD)**, reset at a fixed cadence. Budgets are enforced via per-workspace virtual keys issued by a Kagura-run LiteLLM proxy, with spend tracking and multi-provider fallback handled at the LiteLLM layer.

Inspired by Claude Code's Pro/Max subscription model (flat monthly + weekly cap).

**TL;DR — key decisions** (full rationale below):

- **Reset cadence**: fixed weekly window at UTC Mon 00:00 (not rolling 7-day)
- **Transport**: dedicated endpoints for config + usage; chat ingest reuses Resource Foundation as a **separate epic**
- **Cost ledger**: usage events are **event-shaped**, written to [`llm_call_log`](https://github.com/kagura-ai/memory-cloud/issues/474) with `caller='ai-worker'`. Run-shaped Sleep cost stays in `sleep_reports`; dashboard surfacing UNIONs both via [#472](https://github.com/kagura-ai/memory-cloud/issues/472) (per-shape SoT, unified aggregation)
- **BYOK**: minimum gating = connector seat cap (no Kagura token quota)
- **Gating items**: F1–F6 follow-up issues must be filed before RFC merge

Builds on `Workspace` / `tier` / plan-limit concepts already documented in [docs/concepts.md](../concepts.md#workspace).

## Why now

- `kagura-memory-ai-worker` Phase 2 needs to consume this model
- The current ai-worker README references a "Managed tier billed by `summarization_token_volume`" — this RFC proposes replacing direct metered billing with a subscription + cap model
- LiteLLM provides production-tested primitives (virtual keys, budgets, spend tracking, fallback) — building on it avoids reinventing infrastructure

## Model

### Tier dimensions

Each tier defines:

- Monthly flat fee
- **Weekly LLM budget (USD)** — normalized across providers by LiteLLM, surfaced as `weekly_budget_usd` in the worker contract
- **Connector seat cap** — also referenced as "channel cap" in BYOK contexts
- Feature gates (dedicated VDB, advanced eval, etc.)

Tier ladder (free / starter / pro / max or equivalent) — names and pricing live in a separate commercial decision (see Out of scope).

### Reset semantics

- **Fixed weekly window at UTC Mon 00:00**, not rolling 7-day (see TL;DR; rationale: unambiguous billing + dashboards + customer comms)
- **No inner "session" window** for ai-worker (passive ingest). Future interactive workers (chat-bot) may need finer granularity
- **Overage policy** per-tier:
  - Lower tiers: soft cap (throttle, slower processing)
  - Higher tiers: hard cap (HTTP 429, defer until reset)

### LiteLLM integration

- Deploy LiteLLM proxy as Kagura infrastructure component
- Virtual key per `(workspace_id, week_bucket)` — key format e.g., `wk_<workspace>_<isoweek>`
- Per-key `max_budget` derived from tier definition; LiteLLM returns 429 on exhaustion
- Weekly cron rotates keys (new ISO week → new virtual key, old key revoked)
- Spend tracking exposed via `/api/v1/workspaces/{id}/usage` (LiteLLM passthrough + local idempotent event log)
- Multi-provider routing config: anthropic primary, openai/gemini as fallback on outage

### BYOK mode

- Same LiteLLM endpoint, but virtual key wired to customer's own provider API key (no Kagura-billed cost)
- Spend tracking still emitted (for visibility and upsell signal)
- **Minimum gating**: connector seat cap enforced even on BYOK (see *Resolved open questions → BYOK quota model*)

### Worker contract

`GET /api/v1/workers/{connector_id}/config` returns:

```jsonc
{
  "litellm_endpoint": "https://litellm.kagura.ai/v1",
  "litellm_virtual_key": "wk_<workspace>_<isoweek>",
  "tier": "<tier-identifier>",
  "weekly_budget_usd": 25.0,
  "overage_policy": "soft_throttle",  // enum: "soft_throttle" | "hard_cap"
  "config_version": "<monotonic-int>",
  "valid_until": "<UTC ISO-8601, next rotation boundary>"
}
```

Worker reports usage via `POST /api/v1/workspaces/{id}/usage/events`. See F3 for idempotency-key scope and F5 for the worker self-enforcement contract.

**Relationship to existing `get_usage` MCP tool**: the new REST endpoint and the existing `get_usage` MCP tool ([docs/concepts.md](../concepts.md)) serve different consumers and read from different backends. `get_usage` reports workspace quota counts (memories, contexts, members, MCP calls) sourced from the `memories` / `contexts` / `workspace_members` tables. `/api/v1/workspaces/{id}/usage/events` is the worker→server write path for cost events; those rows land in `llm_call_log` (the event-shaped LLM cost ledger from [#474](https://github.com/kagura-ai/memory-cloud/issues/474)) with `caller='ai-worker'`. Dashboard cost surfacing happens via [#472](https://github.com/kagura-ai/memory-cloud/issues/472)'s UNION ALL of `sleep_reports` (run-shaped Sleep cost) and `llm_call_log` (event-shaped non-Sleep cost), so the per-table split is invisible at the aggregation layer.

## Worker self-enforcement

The cold-start budget guardrail — cold-start backfill MUST consume ≤70% of the weekly budget, leaving ≥30% headroom for steady-state ingest — is **worker-side self-enforcement**, not server-enforced.

- The server (LiteLLM proxy + memory-cloud API) cannot distinguish cold-start from steady-state at the LLM-call layer. From the proxy's perspective, every call is identical: a virtual-key-authenticated request against `max_budget`.
- Server enforcement is limited to the `max_budget` cap on the weekly virtual key. LiteLLM returns HTTP 429 only when the full weekly budget is exhausted, not when the 70% cold-start sub-cap is crossed.
- The 70/30 split is enforced by the worker, before the LiteLLM call, using local accounting (token count × per-model unit price = USD estimate).
- **Misattribution risk**: if cold-start exhausts more than 70% (e.g., a bug in the worker's accounting), the worker burns its own steady-state runway and degrades into 429-driven throttle for the remainder of the week. This is documented worker behavior, not a server bug, and not a refund condition.

**Consumer-side ratification**: `kagura-memory-ai-worker` README MUST echo this contract from the consumer side. Filed as a follow-up against the ai-worker repo (cross-repo edit out of scope for this PR).

## Transport boundary (hybrid design clarification)

This RFC's transport contract uses **dedicated endpoints**, not the Resource Foundation surface. The boundary matters because both layers exist in this codebase and an unintentional conflation creates real semantic damage.

| Concern | Backend | Why not Resource API |
|---|---|---|
| Config delivery (`/api/v1/workers/{connector_id}/config`) | Dedicated endpoint reading from `workspace_connectors` row | The Resource Ingest API itself supports `X-Resource-API-Key` token auth (see `backend/src/api/routes/resource_ingest.py`), so the worker COULD authenticate there. But the schema-registration path (`POST /resources/{id}/schema`) is session-cookie only and so a worker cannot self-bootstrap a schema. More fundamentally, `resource_schemas.field_definitions` is **payload field-type metadata, not runtime config values** — forcing runtime config into schemas would create monotonic version inflation on every tier change and abuse the immutable-per-version contract. The schema-vs-config semantic mismatch is the load-bearing reason, not the auth path |
| Usage reporting (`/api/v1/workspaces/{id}/usage/events`) | Dedicated endpoint writing **event-shaped rows** to [`llm_call_log`](https://github.com/kagura-ai/memory-cloud/issues/474) with `caller='ai-worker'` (and `paid_by` mirroring the existing platform/byok axis). Run-shaped Sleep cost continues to live in `sleep_reports`; [#472](https://github.com/kagura-ai/memory-cloud/issues/472) UNIONs both for dashboard aggregation. **Prerequisite migration**: extend `llm_call_log.caller` CHECK constraint (currently allows the values `'recall'`, `'rerank'`, `'ask'`, `'admin'`, `'sleep'` per `backend/src/models/llm_call_log.py`) to include `'ai-worker'`, and add the corresponding writer-side assertion in `services/llm_call_log_writer.py`. This is an **implementation prerequisite for this RFC's usage/events endpoint**, NOT part of F6 (which is the chat-ingest Resource Foundation reuse epic). It will be filed as its own follow-up issue when the endpoint is implemented. | `resource_events` is indexer-bound business state (Slack messages, Jira tickets), not operational metering. Routing token cost through it either pollutes the indexer pipeline or requires per-event skip logic. The event-shaped vs run-shaped split (`llm_call_log` vs `sleep_reports`) is already the canonical pattern (#474 docstring), unified at the dashboard layer via #472 |
| Chat ingest (Slack/Discord/Teams message ingestion) | **Resource Foundation** (`resources` / `resource_events` / `resource_schemas` / `resource_tokens` / `indexer_state`) — to be implemented as a separate epic | Chat messages ARE business state. The 80% overlap with Resource Foundation makes reuse the right call here. This is out of scope for #693 (transport contract) and will be tracked as a separate epic when ai-worker reaches that phase |

## Resolved open questions

- **Reset timezone**: **UTC fixed (Mon 00:00 UTC reset)**. Simpler than customer-local, and unambiguous in dashboards / comms. Customer-anniversary deferred (would need per-workspace state).
- **Inner session granularity**: **Not needed** for ai-worker (passive ingest). Re-evaluate when an interactive worker (chat-bot) is proposed.
- **BYOK quota model**: connector seat cap is the minimum gate (consistent with memory-analysis access-gate chain). No Kagura token quota — BYOK pays their own provider; only Kagura-side resource cost needs containment.
- **Provider failover cost variance**: USD-denominated normalization is the customer contract. Customer-visible "tokens used" may surprise but is documented in the dashboard tooltip. **No customer-visible per-provider breakdown in v1** (deferred).

## Deferred open questions (separate issues required)

- **LiteLLM operations** (k8s self-host vs LiteLLM Cloud): separate ops issue. Default proposal is self-host for vendor neutrality, but blast-radius + on-call cost tradeoff needs COO review.
- **Multi-tenant LiteLLM blast radius** (single proxy vs per-tier): start with single proxy in v1; per-tier segregation is a follow-up triggered by either (a) per-tier SLA differentiation, or (b) blast-radius incident.

## Follow-up implementation issues (to file before RFC merge)

The table's *Must resolve before* column captures the gating relationship; resolution can come later but the issue must exist:

| # | Issue | Item | Must resolve before |
|---|---|---|---|
| F1 | [#750](https://github.com/kagura-ai/memory-cloud/issues/750) | Tier resolution TTL spec (`config_version` + `valid_until` semantics; mid-week tier change immediacy) | Phase 3 config endpoint implementation |
| F2 | [#751](https://github.com/kagura-ai/memory-cloud/issues/751) | LiteLLM virtual key rotation grace window (in-flight requests must not race with revoke; 5-min default) | Phase 3 LiteLLM deploy |
| F3 | [#752](https://github.com/kagura-ai/memory-cloud/issues/752) | `summary_id` workspace-uniqueness contract (idempotency key scope) | ai-worker `#24` (billing emit) |
| F4 | [#753](https://github.com/kagura-ai/memory-cloud/issues/753) | LiteLLM proxy 5xx degradation policy (v1: stop ingest; Max-tier direct-fallback deferred) | v1 launch |
| F5 | [#754](https://github.com/kagura-ai/memory-cloud/issues/754) | Worker self-enforcement responsibility (cold-start guardrail is worker-side; documentation-only) — see [Worker self-enforcement](#worker-self-enforcement) | RFC merge ✓ |
| F6 | [#755](https://github.com/kagura-ai/memory-cloud/issues/755) | Chat ingest = Resource Foundation reuse epic (`workspace_connectors` + 1:1 mapping to `resources`) | ai-worker Phase 3 |

## Acceptance criteria for this RFC

- [x] Tier dimension model agreed (fields, not values)
- [x] Reset semantics decided (fixed weekly UTC)
- [x] LiteLLM virtual-key issuance and rotation flow specified (with F2 deferred)
- [x] Worker contract response schema specified
- [x] Usage reporting endpoint specified (`/api/v1/workspaces/{id}/usage/events`, writing event-shaped rows to `llm_call_log` with `caller='ai-worker'`)
- [x] Open questions resolved or explicitly deferred with rationale
- [x] Hybrid design boundary specified (dedicated endpoints for config + usage, Resource Foundation for chat ingest)
- [x] Worker self-enforcement contract documented (cold-start guardrail; F5)
- [x] Follow-up issues F1–F6 filed in GitHub (#750–#755)

## Out of scope (separate issues)

- Specific tier pricing and names (commercial decision)
- Stripe integration and invoicing
- Customer-facing usage dashboard UI/UX
- Migration plan for existing Managed-tier customers (none exist — pre-launch)
- LiteLLM deployment automation (separate ops issue)
- Per-provider cost breakdown surfacing in dashboard

## Related

- `kagura-ai/kagura-memory-ai-worker` (consumer worker; #2 LiteLLM migration deferred pending this RFC, see savepoint memory)
- [#474](https://github.com/kagura-ai/memory-cloud/issues/474) `llm_call_log` event-shaped LLM cost ledger (write target for ai-worker usage events)
- [#472](https://github.com/kagura-ai/memory-cloud/issues/472) Cost dashboard UNION ALL of `sleep_reports` + `llm_call_log` (unified aggregation surface)
- LiteLLM docs: https://docs.litellm.ai
- Claude Code subscription model (reference inspiration)
