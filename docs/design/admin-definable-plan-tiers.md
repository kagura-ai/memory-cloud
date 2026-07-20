# Admin-Definable Plan Tiers (Beyond Hardcoded S/M/L)

> **Status: design note (#1394) — not implemented.** Plan tiers are hardcoded
> to three frozen dataclasses (`PLAN_FREE` / `PLAN_BASIC` / `PLAN_PRO` in
> `backend/src/config/plan_tiers.py`, display names "S" / "M" / "L") with
> env-var limit overrides applied at import time
> (`_apply_settings_overrides()`), and frontend display names resolved from
> `NEXT_PUBLIC_PLAN_*` env indirection (`frontend/src/lib/utils/planLabel`).
> Adding a tier requires a code change. This note makes tiers **data**.

## DB-backed plan definitions

```
plan_definitions
  key           str PK     -- stable identifier: "free" | "basic" | "pro" | new keys
  display_name  str        -- human label ("S", "M", "L", ...)
  limits        JSONB      -- the PlanTier field set (max_contexts_per_workspace,
                           -- memory_limit, mcp_calls_per_day, max_connectors,
                           -- storage_limit_bytes, caps, features, ...)
  archived_at   timestamp | null
  created_at / updated_at
```

- **Seed `free`/`basic`/`pro`** from today's dataclass values (display names
  S/M/L) for full backward compatibility — existing `workspaces.plan_name`
  values keep resolving. Note: `plan_name` today carries
  `CheckConstraint("plan_name IN ('free','basic','pro')")` — the migration
  that introduces `plan_definitions` must replace that constraint with an FK
  to `plan_definitions.key`, or dynamic tiers can never be assigned.
- `PlanTier` (the frozen dataclass) stays as the in-process shape;
  `get_plan_tier()` becomes a cached DB read that materializes a `PlanTier`
  from the row. Every quota consumer (`effective_*`, quota services) is
  untouched — they already go through `get_plan_tier()` / `_plan_tier`.
- Unknown-key fallback: a workspace pointing at a missing/archived key
  resolves to its seeded equivalent if one exists, else fails loud (a
  misconfigured tier must not silently become "free").

## Admin CRUD (system admin only)

- Create / edit / **archive** — never delete while any workspace references
  the key. Archive hides the tier from assignment lists; existing workspaces
  keep their limits until migrated.
- Limit edits take effect on the next cache refresh (short TTL, e.g. 60s) —
  no restart.
- The admin plans page renders the dynamic list (it already renders per-tier
  values; the source becomes the API instead of build-time constants).

## Env override interplay (`PLAN_*`) — decision

Self-hosted operators rely on `plan_basic_max_contexts`-style settings today.
Options considered:

1. Env stays a **final override layer** on top of DB rows (status quo
   semantics, two sources of truth forever).
2. Env becomes **bootstrap-seed-only**: values apply once when seeding an
   empty `plan_definitions` table; after that, the DB is authoritative and
   env changes are ignored (with a startup log line when an env var differs
   from the DB row, so drift is visible, never silent).

**Recommendation: (2) seed-only, with the drift warning.** A final-override
layer would make the admin CRUD lie (edits silently masked by env), which is
worse than a one-time migration story. Self-hosted operators keep their
current workflow at first boot and gain the UI afterwards. The transition
release notes must state this explicitly (the "don't break silently"
requirement).

## Frontend display

Plan names and limits are served by the API (extend the existing plans/usage
endpoints with `display_name` + limits from the definition). The
`NEXT_PUBLIC_PLAN_DISPLAY_NAMES` / `NEXT_PUBLIC_PLAN_*_DISPLAY_NAME`
indirection is dropped once all consumers read the API — build-time env for
runtime-editable data is the wrong binding.

## External contract: keys are the stable identifiers

- The private payment service maps prices onto **plan keys**. Keys are
  immutable once created (rename = new key + migration); `display_name` is
  freely editable.
- Key vocabulary is the whole cross-service contract — **no pricing data in
  this repo** (same boundary as the [add-on lane](addon-entitlements.md)).
- Workspace migration between tiers is an explicit admin/billing action
  (`workspaces.plan_name` update), never implicit via key edits.

## Interaction with add-ons

Base tier defines defaults; the [add-on lane](addon-entitlements.md) stacks
on top via the existing `effective_* = _zero_floor(base, Σ addons)` layer.
That resolution order stays defined in exactly one place — tiers becoming
data must not introduce a second stacking site.

## Non-goals

- No per-workspace bespoke limits outside the tier + add-on system (that's
  what add-on grants are for).
- No self-serve tier switching in this repo (billing-service concern).
- No feature-flag redesign — `features: frozenset[str]` rides along in
  `limits` JSONB as a string list.

Refs: #1393, #238, #485, #709, #850.
