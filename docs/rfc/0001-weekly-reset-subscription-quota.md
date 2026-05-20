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

Worker reports usage via `POST /api/v1/workspaces/{id}/usage/events`. See [Usage event idempotency](#usage-event-idempotency) for the `(connector_id, summary_id)` key scope and [Worker self-enforcement](#worker-self-enforcement) for the cold-start guardrail responsibility split.

**Relationship to existing `get_usage` MCP tool**: the new REST endpoint and the existing `get_usage` MCP tool ([docs/concepts.md](../concepts.md)) serve different consumers and read from different backends. `get_usage` reports workspace quota counts (memories, contexts, members, MCP calls) sourced from the `memories` / `contexts` / `workspace_members` tables. `/api/v1/workspaces/{id}/usage/events` is the worker→server write path for cost events; those rows land in `llm_call_log` (the event-shaped LLM cost ledger from [#474](https://github.com/kagura-ai/memory-cloud/issues/474)) with `caller='ai-worker'`. Dashboard cost surfacing happens via [#472](https://github.com/kagura-ai/memory-cloud/issues/472)'s UNION ALL of `sleep_reports` (run-shaped Sleep cost) and `llm_call_log` (event-shaped non-Sleep cost), so the per-table split is invisible at the aggregation layer.

## Worker self-enforcement

The cold-start budget guardrail — cold-start backfill MUST consume ≤70% of the weekly budget, leaving ≥30% headroom for steady-state ingest — is **worker-side self-enforcement**, not server-enforced.

- The server (LiteLLM proxy + memory-cloud API) cannot distinguish cold-start from steady-state at the LLM-call layer. From the proxy's perspective, every call is identical: a virtual-key-authenticated request against `max_budget`.
- Server enforcement is limited to the `max_budget` cap on the weekly virtual key. LiteLLM returns HTTP 429 only when the full weekly budget is exhausted, not when the 70% cold-start sub-cap is crossed.
- The 70/30 split is enforced by the worker, before the LiteLLM call, using local accounting (token count × per-model unit price = USD estimate).
- **Misattribution risk**: if cold-start exhausts more than 70% (e.g., a bug in the worker's accounting), the worker burns its own steady-state runway and degrades into 429-driven throttle for the remainder of the week. This is documented worker behavior, not a server bug, and not a refund condition.

**Consumer-side ratification**: `kagura-memory-ai-worker` README MUST echo this contract from the consumer side. To be filed as a follow-up against the ai-worker repo (cross-repo edit out of scope for this PR).

## Usage event idempotency

Worker reports usage via `POST /api/v1/workspaces/{id}/usage/events`. The idempotency key is the tuple `(connector_id, summary_id)`.

### `summary_id` scope

`summary_id` MUST be **unique within a workspace, across all `connector_id` values**. Workspace-scoped uniqueness, NOT global.

**Rationale**: workspace-scoped uniqueness on the idempotency key matches the scope of the enforcement boundary (`llm_call_log` rows are per-workspace) and prevents the class of bug where a key unique at a different scope (e.g., global or per-connector) can collide or replay across workspaces. The match between key scope and storage scope is the load-bearing property; widening the key (global) loses workspace isolation, and narrowing it (per-connector) reopens the cross-connector replay path.

### Server-side validation

On `POST /api/v1/workspaces/{id}/usage/events`, the server:

1. Resolves the calling worker's `connector_id` and `workspace_id` from the auth context (`X-Resource-API-Key` → `workspace_connectors` row).
2. Checks `llm_call_log` for an existing row matching `(workspace_id, summary_id, caller='ai-worker')`.
3. If found and `connector_id` matches → idempotent replay; respond `200 OK` with the original row's id (no double-write).
4. If found and `connector_id` differs → scope collision; respond `409 Conflict` with body matching the canonical `MemoryCloudException` shape: `{"error": "summary_id_collision", "message": "summary_id already used by a different connector in this workspace", "details": {"connector_id": "<calling-connector-id>", "existing_connector_id": "<owner-connector-id>", "summary_id": "<colliding-id>"}}`. The collision is a worker-side bug (UUID v7 collision is astronomically unlikely; this almost always means two workers misconfigured to share `summary_id` generation state).
5. If not found → insert new row, respond `201 Created`.

The dedupe check is enforced by a partial unique index on `llm_call_log(workspace_id, summary_id) WHERE caller = 'ai-worker'`. The `summary_id` column itself is also new to `llm_call_log` (the current schema, per `backend/src/models/llm_call_log.py`, does not have this column). Both the column addition and the partial unique index are part of the prerequisite migration filed alongside the `caller` CHECK-constraint extension (separate follow-up; see Transport boundary section's note on the `'ai-worker'` value addition).

**Concurrency**: the 5-step sequence above is descriptive of the worker-visible semantics, NOT the prescribed implementation. The check-then-insert pattern (SELECT in step 2, then INSERT in step 5) races under concurrent requests with the same `(workspace_id, summary_id)`; implementations MUST rely on the partial unique index for atomicity instead. The canonical pattern is `INSERT ... ON CONFLICT (workspace_id, summary_id) WHERE caller = 'ai-worker' DO NOTHING RETURNING id` followed by a SELECT on RETURNING-null to retrieve the existing row and branch on `connector_id` per steps 3–4. The equivalent try/except `IntegrityError` pattern is also acceptable. Either way: the unique index is the load-bearing primitive, the explicit SELECT is the contract narrative.

### Worker SDK guidance

The reference SDK (`kagura-memory-python-sdk`) MUST generate `summary_id` as **UUID v7** (time-ordered, 128-bit, monotonic-per-process, globally unique).

Rejected alternatives:

- **Deterministic hash of payload** (e.g., SHA-256 of message body): ties `summary_id` to content, breaking idempotent retries when the worker enriches a payload between attempts (e.g., adding parent-message context). Two retries of the same logical event with differing payload content would compute different hashes, breaking idempotency.
- **Per-connector counter**: requires stateful counter persistence on the worker (durable to crashes, monotonic across instances). UUID v7 is stateless and gives the same time-ordering benefit without the durability burden.

A reference helper will live in `kagura-memory-python-sdk` as `kagura_memory.usage.new_summary_id() -> str` once the SDK adds the `report_usage` method (tracked outside this RFC).

## Config TTL and tier-change immediacy

This section specifies the semantics of `config_version` / `valid_until` from the worker contract response and the timing rules for mid-week tier changes.

### Worker refresh cadence

The worker MUST refresh `GET /api/v1/workers/{connector_id}/config`:

1. **On startup**, before any LiteLLM call.
2. **Every 5 minutes** during steady-state operation (lightweight poll; the endpoint reads from a single `workspace_connectors` row).
3. **Immediately on receipt of `401` / `403` / `429` from the LiteLLM proxy**, treating those as a hint that the cached virtual key may have been rotated or revoked between polls.
4. **Immediately when the cached `valid_until` is reached**, with a 30-second skew tolerance to absorb clock drift.

The 5-minute steady-state poll is a ceiling, not a guarantee — the worker MAY poll more frequently if it observes its own elevated error rate. The server MUST tolerate a poll-per-second rate per connector without throttling. The 1 Hz per-connector tolerance MUST is sized for Phase 3's expected connector cardinality (≤100 connectors total in v1); rate-limit policy MAY be revised once connector counts exceed that.

### `config_version` semantics

`config_version` is a **monotonic non-decreasing integer**, scoped per `connector_id`. Server bumps it whenever ANY field in the response changes: `tier`, `weekly_budget_usd`, `litellm_virtual_key`, `overage_policy`, or `valid_until`.

- Worker SHOULD compare incoming `config_version` against cached value; if equal, no state change needed.
- Worker MUST NOT use `config_version` as a security boundary (it is not signed). It is a freshness hint only.
- Server-side implementation: monotonic counter on `workspace_connectors.config_version`, incremented in the same transaction as any update to the row.

### `valid_until` semantics

`valid_until` is an ISO-8601 UTC timestamp indicating **the latest time at which the worker MAY safely treat its cached config as fresh without re-polling**. After `valid_until`, the worker MUST refresh before issuing further LiteLLM calls (see [Worker refresh cadence](#worker-refresh-cadence)).

The current `litellm_virtual_key` is NOT guaranteed to remain accepted until `valid_until` — it MAY be revoked earlier under any of the following events, all of which the worker only learns about via the next config poll or via a 401/403 from LiteLLM:

1. **Manual key rotation** (operator action, security incident).
2. **Mid-week tier downgrade** with immediate revocation policy (see below).
3. **Workspace deletion or connector unlink** (key revoked at action time).
4. **Weekly cron rotation at the ISO-week boundary** (the routine case; see [Virtual key rotation and grace window](#virtual-key-rotation-and-grace-window)).

Typically `valid_until` equals the next ISO-week boundary (next Mon 00:00 UTC). Worker behavior on early invalidation is governed by the grace window described in F2.

### Mid-week tier change immediacy

| Direction | Server behavior | Worker-visible effect |
|---|---|---|
| **Downgrade (Pro → Free)** | Revoke old virtual key immediately; issue new key with **`max_budget = tier.weekly_budget_usd − already_consumed_usd`** (carry-over enforced at key-creation time, so the new key inherits the prior week's spend). `config_version` bumps. `valid_until` updates to next week boundary as normal. | On next poll (≤5 min) the worker reads the new lower `weekly_budget_usd` and either: continues with already-consumed spend counted against the lower budget (likely already over → 429s until reset), OR if under the new budget, continues normally. |
| **Upgrade (Free → Pro)** | Revoke old virtual key immediately; issue new key with **`max_budget = pro_tier.weekly_budget_usd − already_consumed_usd`** (carry-over enforced at key-creation time; the upgrade increases the cap but does not refund consumed spend). `config_version` bumps. | On next poll the worker reads the higher `weekly_budget_usd` and gains immediate runway. |

**Rationale**: customer-initiated tier changes are intentional admin actions; honoring them at the next week boundary creates a confusing "I upgraded but it didn't help" gap. Both directions take effect at the next worker poll (worst case 5 min delay), but server-side revocation is immediate so the old key cannot continue spending against the wrong budget.

### Cache TTL split

- **Server-side**: no read-through cache. The config endpoint reads `workspace_connectors` direct on every request. The row is single-PK lookup, sub-millisecond — caching adds invalidation complexity for negligible latency gain.
- **Worker-side**: cache the full response with TTL = `min(valid_until - now, 5 minutes)`. Force-refresh on any LiteLLM error per the cadence rules above.

### Reference implementation outline

The server-side endpoint will live at `backend/src/api/routes/workers.py` (new file, follows the pattern of `backend/src/api/routes/resource_ingest.py`). The following outline references names that do not exist yet — `WorkerConfigResponse`, `TierDefinition`, and `authenticate_worker` are aspirational; today's nearest equivalents are the `PlanTier` dataclass + `get_plan_tier(plan_name)` lookup in `backend/src/config/plan_tiers.py` (`weekly_budget_usd` / `overage_policy` are Phase 3 field additions) and the `verify_resource_token` dependency in `resource_ingest.py` (the worker variant will need its own connector-scoped resolver):

```python
@router.get(
    "/api/v1/workers/{connector_id}/config",
    response_model=WorkerConfigResponse,
)
async def get_worker_config(
    connector_id: str,
    db: AsyncSession = Depends(get_db),
    connector: WorkspaceConnector = Depends(authenticate_worker),  # X-Resource-API-Key path
) -> WorkerConfigResponse:
    # IDOR guard: authenticate_worker validates the key but not the path-vs-key
    # alignment. Without this check, a valid key for connector A could request
    # config for connector B by varying the path placeholder.
    if connector.id != connector_id:
        raise AuthorizationError("connector_id does not match authenticated connector")

    workspace = await db.get(Workspace, connector.workspace_id)
    tier = TierDefinition.for_plan(workspace.plan)  # lookup against tier table

    return WorkerConfigResponse(
        litellm_endpoint=settings.LITELLM_ENDPOINT,
        litellm_virtual_key=connector.current_virtual_key,
        tier=tier.identifier,
        weekly_budget_usd=tier.weekly_budget_usd,
        overage_policy=tier.overage_policy,
        config_version=connector.config_version,
        valid_until=connector.virtual_key_valid_until,
    )
```

Schema and migration for `workspace_connectors` (`current_virtual_key`, `config_version`, `virtual_key_valid_until`) are out of scope for this RFC and filed as a Phase 3 implementation issue.

## Virtual key rotation and grace window

Weekly cron rotates each workspace's virtual key on the ISO-week boundary (Mon 00:00 UTC). To prevent in-flight requests from racing with revocation, the **old key remains valid for a 5-minute grace window** after rotation.

### Grace window duration

**Fixed at 5 minutes in v1.** Per-tier configurability is deferred to v2; the worker's poll cadence (5 min steady-state, see [Config TTL](#config-ttl-and-tier-change-immediacy)) means any tier's worker will pick up the new key within the grace window under normal operation.

### Rotation sequence

At T-0 (cron fires, e.g., Mon 00:00:00 UTC for ISO week 23):

1. Call `POST /key/sk-{old_key_hash}/regenerate` against the LiteLLM proxy with `grace_period: "5m"`. LiteLLM atomically issues a new key `wk_<workspace>_2026W23` with the tier's `max_budget` and keeps the old key valid for 5 minutes. (See [LiteLLM key regeneration docs](https://docs.litellm.ai/docs/proxy/virtual_keys). **Requires LiteLLM Enterprise tier**; if the deployment runs the community edition, the rotation falls back to the wrapper-based dual-key approach described in F2's open question — see below.)
2. Update `workspace_connectors.current_virtual_key` (to the regenerated key) and bump `config_version` in the same database transaction.
3. At T+5min, LiteLLM begins rejecting requests on the old key. The exact rejection status code is upstream-defined (most likely 401 per LiteLLM expiry behavior, but the worker treats any 401/403/429 as a refresh signal per [Config TTL](#config-ttl-and-tier-change-immediacy)'s cadence rules, so the precise code does not affect worker correctness).

### Worker-side behavior during grace

- Worker's cached key remains valid for steady-state calls until next poll picks up the new key.
- On the first poll AFTER T-0 (worst case T+5min minus 30s skew), worker observes `config_version` bumped and switches to the new key.
- If worker receives 401 on the old key (i.e., its poll missed the rotation), it MUST force-refresh config immediately and retry the call once with the new key. Repeated 401 after refresh = legitimate auth failure, surfaced to operator.
- Worker MUST NOT pre-fetch the new key before T-0; the new key's spend would charge against the prior week's budget if the server's `valid_until` updates lag the actual rotation.

### Configuration

| Setting | Value | Where |
|---|---|---|
| Grace window duration | 5 minutes | Kagura rotation-cron config; passed as `grace_period: "5m"` to LiteLLM's `/key/regenerate` API (or to the wrapper described in the Open question below) |
| Rotation cron schedule | `0 0 * * 1` (Mon 00:00 UTC) | Server-side scheduler (k8s CronJob or equivalent) |
| Per-tier override | Not supported in v1 | Deferred to v2 |

### Open question (community-tier LiteLLM fallback)

The atomic `regenerate` + `grace_period` flow described above is a LiteLLM Enterprise feature. If Kagura's production deployment uses the community edition (e.g., during early testing), the rotation must fall back to a Kagura-side dual-key wrapper:

1. At T-0, issue the new key via `POST /key/generate` (community-tier supports this with `duration` for initial expiry).
2. Record the old key as `previous_virtual_key` on `workspace_connectors` with `previous_valid_until = T+5min`.
3. All worker requests pass through a Kagura wrapper that accepts either `current_virtual_key` or (`previous_virtual_key` AND `now < previous_valid_until`).
4. At T+5min, the wrapper rejects `previous_virtual_key`; the old key in LiteLLM is deleted in a background job.

This fallback adds operational complexity (wrapper service in front of LiteLLM). Filed as a separate decision (Enterprise vs community + wrapper) in the deployment ops issue (see [Deferred open questions](#deferred-open-questions)).

## Proxy degradation policy

Specifies worker behavior when the LiteLLM proxy returns HTTP 5xx (proxy or upstream provider failure), distinct from HTTP 429 (quota exhausted), which has its own per-tier overage policy.

### v1 decision: stop ingest

When the worker observes sustained 5xx from the LiteLLM proxy, it **stops ingest** and emits a paused signal. This favors quota correctness over availability: bypassing LiteLLM with a direct provider call would skip spend tracking and corrupt the weekly budget enforcement.

**Direct-fallback (worker → provider direct) is explicitly rejected for v1.** It would:

- Bypass `max_budget` enforcement → unbounded customer spend during incidents.
- Skip the `llm_call_log` event-shaped ledger → cost dashboards under-report.
- Require post-incident reconciliation logic that doesn't exist yet.

### Worker status enum

The worker exposes its operational state via the `workspace_connectors.status` column. The enum extends as follows:

```python
class WorkerStatus(str, Enum):
    RUNNING = "running"
    PAUSED_PROXY_5XX = "paused_proxy_5xx"
    PAUSED_QUOTA_EXHAUSTED = "paused_quota_exhausted"
    PAUSED_MANUAL = "paused_manual"
```

State transitions:

| From | To | Trigger |
|---|---|---|
| `running` | `paused_proxy_5xx` | ≥3 consecutive 5xx responses in 60s window (counter resets on any 2xx) |
| `paused_proxy_5xx` | `running` | First 200 OK from `GET /api/v1/workers/{connector_id}/config` after pause |
| `running` | `paused_quota_exhausted` | LiteLLM 429 with response body containing `"type": "budget_exceeded"` (per `litellm.exceptions.BudgetExceededError`); other 429 bodies (provider rate-limit, `tpm`/`rpm`) trigger config force-refresh per F1 cadence, NOT this transition |
| `paused_quota_exhausted` | `running` | Next ISO-week boundary (`valid_until` reached) |
| any | `paused_manual` | Operator action via admin UI |

### Recovery cadence

When in `paused_proxy_5xx`, the worker polls `GET /api/v1/workers/{connector_id}/config` every **60 seconds** (instead of the 5-minute steady-state cadence). The config endpoint is independent of LiteLLM, so its availability is the primary recovery signal. First successful response → transition to `running`, retry the in-flight ingest item. The 200-OK-from-config recovery trigger applies ONLY to `paused_proxy_5xx`. A worker in `paused_quota_exhausted` MUST NOT exit that state on a 200 OK from config — quota recovery is solely `valid_until`-driven (i.e., next ISO-week boundary), regardless of config endpoint health.

### Customer-visible signals

- **Admin UI**: the workspace connector row surfaces `status` with the human-readable label "Paused — LLM provider degraded" and a tooltip describing the auto-recovery behavior.
- **Worker NDJSON stream**: emits `{"v": 1, "ts": "<ISO-8601>", "stage": "proxy_health", "kind": "warning", "msg": "paused: proxy 5xx", "detail": {...}}` on entry to `paused_proxy_5xx`, and `{"v": 1, "ts": "<ISO-8601>", "stage": "proxy_health", "kind": "success", "msg": "resumed", "detail": {...}}` on recovery. The minimal NDJSON event schema used here: `v` (schema version, required; consumers MUST reject unknown versions), `ts` (ISO-8601 UTC timestamp, required), `stage` (non-exclusive identifier of the operational stage emitting the event, required), `kind` (closed enum `action` / `detail` / `success` / `warning` / `error` / `debug`, required; `success` and `error` are terminal-event kinds), `msg` (human-readable, optional), `detail` (structured payload, optional). Each pause/resume cycle is a separate stage instance (e.g., `proxy_health_1`, `proxy_health_2`); the schema's "one terminal event per stage" rule is preserved by per-cycle stage IDs, not by suppressing repeat pauses.
- **No customer email** in v1 (would be noisy during multi-tenant incidents); deferred to a customer-comms follow-up.

### Per-tier override (deferred)

Max tier may eventually receive a `degradation_policy: "direct_fallback"` capability for higher availability at the cost of post-incident reconciliation. Out of scope for v1; tracked as a separate enhancement issue once Max tier is commercially defined.

### Alerting / dashboard

- **Operator alert**: PagerDuty triggers when the cluster-wide LiteLLM 5xx rate exceeds 5% over a 5-minute window. Threshold tuned during initial deploy; revisited after first incident. Per-tenant alerting is deferred to v2 — the cluster-wide threshold is conservative and will miss low-cardinality single-customer incidents that don't move the global rate.
- **Customer-facing dashboard**: usage page shows a banner if the workspace has any connector in `paused_proxy_5xx`. Banner auto-clears within ~60s of recovery.
- **Cost dashboard impact**: paused workers emit no `llm_call_log` rows, so the usage chart shows a visible gap (helpful as a post-incident artifact).

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
| F1 | [#750](https://github.com/kagura-ai/memory-cloud/issues/750) | Tier resolution TTL spec (`config_version` + `valid_until` semantics; mid-week tier change immediacy) — see [Config TTL and tier-change immediacy](#config-ttl-and-tier-change-immediacy) | Phase 3 config endpoint implementation ✓ |
| F2 | [#751](https://github.com/kagura-ai/memory-cloud/issues/751) | LiteLLM virtual key rotation grace window (in-flight requests must not race with revoke; 5-min default) — see [Virtual key rotation and grace window](#virtual-key-rotation-and-grace-window) | Phase 3 LiteLLM deploy ✓ |
| F3 | [#752](https://github.com/kagura-ai/memory-cloud/issues/752) | `summary_id` workspace-uniqueness contract (idempotency key scope) — see [Usage event idempotency](#usage-event-idempotency) | ai-worker `#24` (billing emit) ✓ |
| F4 | [#753](https://github.com/kagura-ai/memory-cloud/issues/753) | LiteLLM proxy 5xx degradation policy (v1: stop ingest; Max-tier direct-fallback deferred) — see [Proxy degradation policy](#proxy-degradation-policy) | v1 launch ✓ |
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
- [x] Usage event idempotency contract specified (`summary_id` workspace-scope; F3)
- [x] Config TTL and tier-change immediacy specified (`config_version`, `valid_until`, mid-week upgrade/downgrade; F1)
- [x] Virtual key rotation grace window specified (5-min fixed; rotation sequence; dual-key behavior; F2)
- [x] Proxy 5xx degradation policy specified (stop ingest, worker status enum, recovery cadence, alerting; F4)
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
