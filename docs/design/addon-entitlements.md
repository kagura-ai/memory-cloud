# Add-On Entitlement Lane Alongside Base Plan Tiers

> **Status: design note (#1393) — not implemented.** Some capabilities are
> orthogonal to the base tier — connector seats and platform-borne connector
> LLM usage ([#1392](platform-borne-connector-llm.md)) being the immediate
> drivers — and should be grantable as **add-ons** stacked on a base plan
> instead of forcing a tier upgrade.

## What already exists (ground truth, not greenfield)

The *effect* side of an add-on lane already ships:

- `Workspace` carries denormalized bonus columns (Migration 048, #238;
  extended by #15/#485/#494/#560 and the 2026-06-02 connector-seat spec):
  `addon_memory_bonus`,
  `addon_mcp_quota_bonus`, `addon_rest_quota_bonus`,
  `addon_public_quota_bonus`, `addon_member_bonus`, `addon_context_bonus`,
  `addon_analysis_bonus`, `addon_storage_bonus_mb`,
  `addon_sleep_contexts_bonus`, `addon_connector_bonus`.
- Every quota choke point reads `effective_*` properties which stack
  `_zero_floor(plan_tier.<base>, addon_<bonus>)` — a plan base of 0 keeps the
  feature off regardless of add-ons (paid-boundary rule).

What is **missing** is the *grant* side: the bonus columns are bare integers
with no provenance — no who/when/why, no source, no expiry, no audit trail,
and no contract an external billing service can drive.

## Design: a grant ledger upstream of the existing columns

```
workspace_addons
  id             UUID PK
  workspace_id   FK → workspaces
  addon_key      str        -- e.g. "connector_seats", "context_slots",
                            --      "llm_usage_topup_usd"
  quantity       int        -- units of the addon_key's unit
  source         str        -- "system_admin" | "billing"
  granted_by     str | null -- admin user id for system_admin grants
  external_ref   str | null -- billing service's opaque grant reference
  granted_at     tz-aware timestamp
  expires_at     tz-aware timestamp | null
  revoked_at     tz-aware timestamp | null
```

- **The ledger is the source of truth; the existing `addon_*_bonus` columns
  become a projection.** On grant/revoke/expiry, the service recomputes
  `Σ quantity` per addon key and writes the matching bonus column. The 10+
  `effective_*` call sites and `_zero_floor` stay byte-identical — the hot
  path never joins the ledger.
- Expiry is enforced at projection time (a periodic sweep + on-write
  recompute), not per-request.
- Grant/revoke are admin- or billing-driven service calls, never direct
  column writes; the ledger row is the audit record.

## Grant sources — deployment-mode two-sidedness

- **`system_admin`**: manual ops grants. Self-hosted admins can grant freely
  (their server, their rules) — same two-sidedness as env-based plan
  overrides.
- **`billing`**: the external billing service calls a grant contract
  (`POST /api/v1/admin/workspaces/{id}/addons` or equivalent) with an
  `external_ref` for reconciliation. memory-cloud remains the **entitlement
  source of truth**; the billing service holds purchase state.

## Metered add-ons (the #1392 driver)

Static-capacity add-ons (seats, slots) are fully served by the ledger →
projection model. **Metered** add-ons (platform LLM usage top-ups) also need
per-addon *usage* accounting:

- Model on the managed-embeddings **check + record** pattern (#709/#1030/
  #1033): check remaining budget before the metered action, record actual
  usage after.
- The add-on contributes budget (`quantity` in the addon key's unit, e.g.
  USD-cents of LLM usage); usage records deduct against
  plan-cap + Σ active top-ups.

## Boundary (deliberate)

Purchase, checkout, and payment-provider mechanics live in the **private
payment service**. This repo exposes only the grant contract and entitlement
state. **No pricing appears in this repo** — `addon_key` + `quantity` are the
whole vocabulary; what a grant costs is not memory-cloud's concern.

## Admin UI

The admin plans page gains a per-workspace add-on panel: active grants
(key, quantity, source, granted_at, expires_at), grant/revoke actions
(system-admin only), and the projected effective totals next to the base-tier
values it already shows.

## Interaction with admin-definable tiers

The base tier defines defaults; the add-on lane stacks on top. The resolution
order (base → Σ add-ons → zero-floor) is defined **once** at the projection/
`effective_*` layer — the [admin-definable plan tiers](admin-definable-plan-tiers.md)
design keeps that single definition point when tiers become data.

Refs: #1392, #1394, #238, #857.
