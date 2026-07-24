# Workspace management key separation (data keys vs management keys)

> **Status: Phase 1 approved for implementation; Phase 2 designed and
> deferred.** Origin: the initial-setup `admin-cli` key sits in the API-key
> list indistinguishable from ordinary keys, and every key owned by a
> workspace admin/owner silently carries workspace-management authority.
> Design session 2026-07-24; recorded in Kagura Memory `2d5f92c7` (current
> state) and the session savepoint.

## Problem

Two related problems, one operational and one structural:

1. **Operational clarity (acute).** The credentials screen shows all of a
   user's keys as equals. The operator cannot tell which key is wired into a
   live MCP connection (`admin-cli` from `create_admin`), and nothing warns
   before revoking/regenerating a key that is actively in use. The
   "difference in handling" between an admin's key and a member's key is
   invisible even though it is real.
2. **Ambient authority (structural).** A key's power is derived entirely
   from its owner's current workspace role. Every key an admin/owner mints —
   including keys intended purely for data-plane automation (kagura-bridge
   writers, CI ingest, `.mcp.json`) — can also call connector setup, secret
   management, and member management. There is no way to mint a
   least-privilege data-only key as an owner.

## Current state (verified 2026-07-24, v0.59.1)

- API keys (`api_keys`, `kagura_*`) carry **no role of their own**. The
  API-key principal is hardcoded `role: "user"`
  (`auth/dependencies.py::_build_api_key_user_dict`), so **system-admin
  surfaces (`/api/v1/admin/*`, `require_admin`) are structurally
  unreachable via any key** — session-only by design (#166 separation).
  This spec does not change that and never will.
- Workspace-layer gates DO accept keys: `require_workspace_admin` /
  `require_workspace_owner` check the **key owner's** live membership role.
  The member/invitation/member-credential surfaces additionally run
  `authorize_workspace_management` (#1164/#1165): API-key principals are
  **owner-only** there, OAuth bearers are rejected outright.
- The credentials UI (`/workspace/integrations/credentials`, API keys tab)
  is backed by the **member-credentials API**
  (`/api/v1/workspaces/{ws}/members/{uid}/credentials/*`), which mints
  workspace-scoped keys. The legacy `/api/v1/api-keys` routes (session-only)
  mint owner-scoped *global* keys and are not used by the UI.
- Existing key variants: owner-scoped (global), workspace-scoped (#169),
  public-bound (#626, structurally rejected outside the public surface),
  agent-bound (#1275, verify-time kill switch), plus the separate
  `share_keys` table (#1027, read-only surface, fail-closed by table
  separation).
- `create_admin` CLI mints a workspace-scoped key named `admin-cli` for the
  initial admin and writes it into `.mcp.json`. Its privileges are identical
  to any owner-owned key — the "admin" in its name is provenance, not
  capability.

## Decision summary

| Question | Decision |
| --- | --- |
| Restriction target | New **management-key class** for workspace-management APIs (not minting restrictions, not session-only lockdown) |
| Key relationship | **Superset (scope model)**: management key = data plane + management APIs. A single key still serves a full MCP session |
| Migration | **Grandfather**: existing admin/owner-owned keys keep management power via backfill; new keys default to data-only |
| Rollout | **Phase split**: ship operational clarity first (no schema change); implement the capability model when a trigger fires |

Honest security accounting (drove the phase split): with grandfathering, the
automatic day-one security gain is **zero**. The capability model's real
security value is *enabling* data-only owner keys (de-privileging
kagura-bridge / CI credentials) — an opt-in gain realized only when keys are
rotated. Operational clarity, the acute problem, does not need the schema
change at all. Hence Phase 1 ships clarity; Phase 2 ships the mechanism.

---

## Phase 1 — operational clarity (implement now)

Frontend-only; zero backend/schema changes. All data needed is already
served (`MemberCredentials.target_user_role`, `MemberAPIKey.last_used_at`).

### 1.1 Role-derived permission badge

Add a 権限/type column to the API-keys table in `APIKeysTabPanel`:

- Owner role `owner`/`admin` → badge **管理可** (pro tint) on each key row.
- Owner role `member`/`viewer` → badge **データのみ** (neutral).
- Data source: `target_user_role` (uniform across rows today — the column
  deliberately matches Phase 2's UI shape, where it becomes per-key).
- Public-bound keys (#626) show their binding badge **instead of** the role
  badge: a public-bound key cannot authenticate on management surfaces at
  all (structurally rejected outside `/api/v1/public/*`), so a 管理可 badge
  on such a row would be factually wrong.

### 1.2 Panel-level explanation

One help line above the table (and `featureGuide` copy update):

> APIキーは所有者のワークスペースロールの権限で動作します。admin / owner の
> キーはワークスペース管理API(コネクタ・シークレット・メンバー等)も呼び出せ
> ます。

The docs/FAQ anchor for "which APIs are management": **"whatever requires
admin/owner in the browser UI"** — the API boundary is 1:1 with the screen
permission boundary, so users never learn a second boundary.

### 1.3 Destructive-action guard (mis-revoke protection)

In the revoke / regenerate / delete confirmation dialogs: if the key's
`last_used_at` is within **24 hours**, prepend a warning line:

> このキーは{relative time}に使用されています。使用中のクライアント
> (MCP / SDK / worker)の接続が切れます。

This is the direct fix for the "accidentally kill the `admin-cli` key and
silently break `.mcp.json`" hazard. Threshold constant lives beside the
existing visibility constants; `last_used_at` granularity is adequate
(#947 throttling is seconds-scale).

### 1.4 Setup-key provenance note (name-based, documented limitation)

Rows named `admin-cli` get a muted "初期セットアップで作成" hint (tooltip or
sub-label). Name matching is a heuristic — acceptable for Phase 1 because the
guard in 1.3 protects the key regardless; a real `created_via` column is
Phase 2 material if wanted.

### 1.5 i18n and tests

- Keys added to BOTH `frontend/src/messages/en.json` and `ja.json`.
- Vitest: badge rendering per role, guard line presence/absence around the
  24h threshold, i18n key existence. No backend tests (no backend change).

Out of scope for Phase 1: any enforcement change, any schema change, the
create-dialog management toggle (Phase 2), `create_admin` changes.

---

## Phase 2 — capability model (designed, deferred)

### 2.1 Data model

- `api_keys.can_manage_workspace BOOLEAN NOT NULL DEFAULT FALSE`.
- Threaded through `VerifiedKey` and the API-key principal dict.
- Named as a capability (not `is_admin_key`) so a future lift into a scopes
  vocabulary (#649 direction, the `workspace:admin` scope foreshadowed in
  `programmatic_workspace_auth`) is a rename, not a remodel.

### 2.2 Effective-permission rule (fail-closed core)

Effective management power = `can_manage_workspace` **AND** the owner's
**live** role in the target workspace is admin/owner, evaluated at
verify/authorize time. Demotion instantly disables the holder's management
keys (no key sweep), mirroring the #1275 agent kill-switch philosophy. The
flag alone grants nothing.

### 2.3 Enforcement chokepoints (4, no per-route edits)

1. `require_workspace_admin` / `require_workspace_owner`: API-key principals
   lacking the capability → 403. Session principals unchanged.
2. `authorize_workspace_management` (#1164 surfaces): keeps owner-only AND
   adds the capability requirement.
3. MCP management tools (`setup_connector`, `secret_put`, `register_agent`,
   resource-token mutations, `update_search_config`, `rollback_sleep_run`,
   …): one shared guard helper keyed on the principal's capability. The
   exact tool inventory is an implementation-plan task: enumerate MCP tools
   whose REST twins sit behind admin/owner gates. Context CRUD stays
   data-plane (member-level today).
4. Data-plane surfaces: untouched.

### 2.4 Minting and UI

- Member-credentials create endpoint accepts `can_manage_workspace: true`
  only when the **session** caller holds admin/owner in the path workspace
  (same gate family as #1164; OAuth still rejected there).
- Legacy `/api/v1/api-keys` (global keys) does NOT accept the flag —
  data-only forever. This kills "global management keys" without a special
  case: management capability only ever exists on workspace-scoped keys.
- Create dialog: "ワークスペース管理を許可" toggle, visible only to
  admin/owner sessions (client-gated AND server-enforced), with the copy
  mocked in the 2026-07-24 session. Badge column switches its data source
  from `target_user_role` to the per-key flag.
- Owner-provisioned member keys (#1164 mint-for-member) are always
  data-only.
- `create_admin` CLI mints the initial `admin-cli` key WITH the capability
  (operator bootstrap via MCP needs it; name and label finally agree).

### 2.5 Grandfather migration

Alembic backfill, exactly preserving current effective behavior:

- Workspace-scoped keys → `true` iff the owner currently holds admin/owner
  in that workspace.
- Global keys → `true` iff the owner currently holds admin/owner in ANY
  workspace (global keys follow `current_workspace_id` today, so this is
  the faithful translation; combined with 2.2 the live-role check keeps it
  exact per-request).
- Member-owned keys stay `false`. Zero behavior change at cutover; UI shows
  grandfathered keys with a "移行で引き継ぎ" hint so operators can rotate
  toward least privilege at their own pace.

### 2.6 User-facing model (the explainability contract)

1. Every key can read/write memory (remember / recall / explore).
2. A management key can ALSO call workspace-management APIs — the same set
   of operations that requires admin/owner in the browser. Only admin/owner
   can mint one.
3. Effective power = key capability × owner's current role; demotion stops
   the management half immediately, data access survives.

Known wording risk: "management" vs the unrelated **system admin** concept —
UI copy always says ワークスペース管理, never 管理者/システム管理.

### 2.7 Non-goals

- System-admin surfaces stay session-only. No key ever reaches
  `/api/v1/admin/*`.
- No fine-grained per-surface scopes (connectors-only, secrets-only, …) —
  if demanded, lift the boolean into scopes rather than adding booleans.
- No TTL/expiry changes; #889 mechanics are orthogonal and compose.

### 2.8 Phase 2 triggers (implement when the first fires)

1. A team workspace with non-trivial member count goes live (multi-member
   least-privilege becomes real), or
2. an enterprise conversation asks for key privilege separation, or
3. work starts on the v1.0 API surface freeze (#622) — the capability must
   exist before the surface freezes, because retro-tightening auth post-1.0
   is a breaking change.

### 2.9 Phase 2 test plan sketch

Matrix: key kind (grandfathered / minted-with / minted-without) × owner role
(owner / admin / member / demoted-after-mint) × surface (workspace-admin
REST, #1164 member management, MCP management tool, data plane). Regression
pins: #1164 owner-only unchanged for capable keys, #626 public-bound and
#1027 share keys unaffected, demotion kill-switch, legacy route rejects the
flag.

## References

- Issues: #166 (system vs workspace admin split), #169 (workspace-scoped
  keys), #252/#398 (session-only surfaces), #626 (public-bound keys), #649
  (OAuth scopes), #889 (TTL), #1027 (share keys, dedicated-table decision),
  #1164/#1165 (programmatic workspace management, owner-only + OAuth
  rejection), #1275 (agent-bound keys, kill switch), #622 (v1.0 freeze).
- Kagura Memory: `2d5f92c7` (admin-key current-state investigation),
  `43b417b2` / `9ae72264` (share-key dedicated-table rationale),
  `bedfa6e2` (connectors RBAC), `b786a8c5` (bridge prod key usage).
