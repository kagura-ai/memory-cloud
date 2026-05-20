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
- **Cost ledger**: usage events merge into [`sleep_reports`](sleep-maintenance.md) cost rows via [#523](https://github.com/kagura-ai/memory-cloud/issues/523) `source` / `paid_by` columns (single SoT)
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

```json
{
  "litellm_endpoint": "https://litellm.kagura.ai/v1",
  "litellm_virtual_key": "wk_<workspace>_<isoweek>",
  "tier": "<tier-identifier>",
  "weekly_budget_usd": 25.0,
  "overage_policy": "soft_throttle" | "hard_cap",
  "config_version": "<monotonic-int>",
  "valid_until": "<UTC ISO-8601, next rotation boundary>"
}
```

Worker reports usage via `POST /api/v1/workspaces/{id}/usage/events`. See F3 for idempotency-key scope and F5 for the worker self-enforcement contract.

**Relationship to existing `get_usage` MCP tool**: the new REST endpoint and the existing `get_usage` MCP tool ([docs/concepts.md](../concepts.md)) serve different consumers — `get_usage` is for AI clients querying their own workspace quota, while `/api/v1/workspaces/{id}/usage/events` is the worker→server write path. They share the underlying `sleep_reports` cost ledger (see Transport boundary table) but expose separate surfaces; no convergence is required in v1.

## Transport boundary (hybrid design clarification)

This RFC's transport contract uses **dedicated endpoints**, not the Resource Foundation surface. The boundary matters because both layers exist in this codebase and an unintentional conflation creates real semantic damage.

| Concern | Backend | Why not Resource API |
|---|---|---|
| Config delivery (`/api/v1/workers/{connector_id}/config`) | Dedicated endpoint reading from `workspace_connectors` row | `resource_schemas.field_definitions` is payload field-type metadata, not runtime config values. Forcing runtime config into schemas would create monotonic version inflation on every tier change, requires session-cookie auth (worker cannot self-bootstrap), and abuses the immutable-per-version contract |
| Usage reporting (`/api/v1/workspaces/{id}/usage/events`) | Dedicated endpoint writing to **`sleep_reports` cost rows** (`source='ai-worker'`, reusing [#523](https://github.com/kagura-ai/memory-cloud/issues/523) `source` / `paid_by` columns) | `resource_events` is indexer-bound business state (Slack messages, Jira tickets), not operational metering. Routing token cost through it either pollutes the indexer pipeline or requires per-event skip logic. A unified cost ledger (single SoT) matches the existing memory-analysis / sleep-maintenance pattern |
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
| F5 | [#754](https://github.com/kagura-ai/memory-cloud/issues/754) | Worker self-enforcement responsibility (cold-start guardrail is worker-side; documentation-only) | RFC merge |
| F6 | [#755](https://github.com/kagura-ai/memory-cloud/issues/755) | Chat ingest = Resource Foundation reuse epic (`workspace_connectors` + 1:1 mapping to `resources`) | ai-worker Phase 3 |

## Acceptance criteria for this RFC

- [x] Tier dimension model agreed (fields, not values)
- [x] Reset semantics decided (fixed weekly UTC)
- [x] LiteLLM virtual-key issuance and rotation flow specified (with F2 deferred)
- [x] Worker contract response schema specified
- [x] Usage reporting endpoint specified (`/api/v1/workspaces/{id}/usage/events`, merging into `sleep_reports` cost rows)
- [x] Open questions resolved or explicitly deferred with rationale
- [x] Hybrid design boundary specified (dedicated endpoints for config + usage, Resource Foundation for chat ingest)
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
- [#523](https://github.com/kagura-ai/memory-cloud/issues/523) `sleep_reports.source/paid_by` columns (cost ledger pattern reused)
- LiteLLM docs: https://docs.litellm.ai
- Claude Code subscription model (reference inspiration)
