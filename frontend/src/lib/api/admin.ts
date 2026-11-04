/**
 * Admin API Client
 *
 * Issue #149: Plan tier enforcement - Admin management
 */

import { ApiError, apiClient } from "./base";
import { ContextRole, WorkspaceRole } from "@/lib/auth/rbac";
import type { PlanTier } from "@/lib/utils/planLabel";

/**
 * The admin user page for a stable ``user_id`` (OAuth2 sub). Ids may carry
 * ``:`` (``local:admin``, connector identities), so the segment is encoded.
 * One place for every admin table that links a user (#1861).
 */
export function adminUserPath(userId: string): string {
  return `/admin/users/${encodeURIComponent(userId)}`;
}

/** A member of a workspace in the successor picker (the user being deleted left out). */
export interface TransferMember {
  user_id: string;
  name: string | null;
  email: string | null;
  role: string;
}

/** A workspace the picker lists: one the user owns, or one holding a listed context. */
export interface TransferWorkspace {
  id: string;
  name: string;
  owner_user_id: string;
  members: TransferMember[];
}

/**
 * A context the user created that needs a successor. `candidate_user_ids` are
 * the members an explicit pick may name; the successor picked for its
 * workspace (where the user owns it) is accepted too.
 */
export interface TransferContext {
  id: string;
  name: string;
  display_name: string | null;
  workspace_id: string;
  is_private: boolean;
  candidate_user_ids: string[];
}

/**
 * A connector's write target: it goes to the workspace's owner and takes no
 * selection. `successor_user_id` is that owner, or `null` when it is the
 * successor picked for a workspace the user owns.
 */
export interface OwnerFixedContext {
  id: string;
  name: string;
  display_name: string | null;
  workspace_id: string;
  successor_user_id: string | null;
}

/** What the successor picker shows, from the 409 (#2420). */
export interface TransferPicker {
  /** The owned workspaces that need a successor (rows of the picker). */
  workspaceIds: string[];
  workspaces: TransferWorkspace[];
  contexts: TransferContext[];
  ownerFixedContexts: OwnerFixedContext[];
}

/**
 * Why `DELETE /api/v1/admin/users/{user_id}` failed (#2417). Since #2372 the
 * route answers 409 with the owned workspaces and shared contexts that still
 * need a successor (`details.detail`); since #2420 that 409 also carries the
 * successor picker (`picker`, absent from an older API, which leaves the
 * counts). Other refusals, such as a running subscription (`BILLING-005`) or
 * admin protection, carry their reason in `message`.
 */
export type UserDeleteFailure =
  | {
      kind: "transfer_required";
      workspaces: number;
      contexts: number;
      picker?: TransferPicker;
    }
  | { kind: "message"; message: string }
  | { kind: "unknown" };

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string")
    : [];
}

/** The entries of `value` that `parse` accepts; a malformed one is dropped. */
function entries<T>(value: unknown, parse: (entry: Json) => T | null): T[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    const parsed = isObject(entry) ? parse(entry) : null;
    return parsed === null ? [] : [parsed];
  });
}

function parseMember(m: Json): TransferMember | null {
  const userId = str(m.user_id);
  return userId === null
    ? null
    : {
        user_id: userId,
        name: str(m.name),
        email: str(m.email),
        role: str(m.role) ?? "",
      };
}

function parseWorkspace(w: Json): TransferWorkspace | null {
  const id = str(w.id);
  return id === null
    ? null
    : {
        id,
        name: str(w.name) ?? id,
        owner_user_id: str(w.owner_user_id) ?? "",
        members: entries(w.members, parseMember),
      };
}

function contextBase(c: Json) {
  const id = str(c.id);
  const workspaceId = str(c.workspace_id);
  return id === null || workspaceId === null
    ? null
    : {
        id,
        name: str(c.name) ?? id,
        display_name: str(c.display_name),
        workspace_id: workspaceId,
      };
}

function parseContext(c: Json): TransferContext | null {
  const base = contextBase(c);
  return base === null
    ? null
    : {
        ...base,
        is_private: c.is_private === true,
        candidate_user_ids: strings(c.candidate_user_ids),
      };
}

function parseOwnerFixed(c: Json): OwnerFixedContext | null {
  const base = contextBase(c);
  return base === null
    ? null
    : { ...base, successor_user_id: str(c.successor_user_id) };
}

export function userDeleteFailure(error: unknown): UserDeleteFailure {
  if (!(error instanceof ApiError)) {
    return { kind: "unknown" };
  }
  const detail: unknown = error.details?.detail;
  if (isObject(detail)) {
    const { workspace_ids, context_ids } = detail;
    if (Array.isArray(workspace_ids) || Array.isArray(context_ids)) {
      const failure: UserDeleteFailure = {
        kind: "transfer_required",
        workspaces: Array.isArray(workspace_ids) ? workspace_ids.length : 0,
        contexts: Array.isArray(context_ids) ? context_ids.length : 0,
      };
      // An API before #2420 sends the ids only: no picker, the counts stay.
      if (Array.isArray(detail.workspaces) && Array.isArray(detail.contexts)) {
        failure.picker = {
          workspaceIds: strings(workspace_ids),
          workspaces: entries(detail.workspaces, parseWorkspace),
          contexts: entries(detail.contexts, parseContext),
          ownerFixedContexts: entries(
            detail.owner_fixed_contexts,
            parseOwnerFixed,
          ),
        };
      }
      return failure;
    }
  }
  // Only a refusal (4xx) carries a reason worth showing; a 5xx or a dropped
  // connection gets the generic message.
  const refused = error.status >= 400 && error.status < 500;
  return refused && error.message && error.message !== "Request failed"
    ? { kind: "message", message: error.message }
    : { kind: "unknown" };
}

export interface WorkspacePlanInfo {
  id: string;
  name: string;
  plan_name: string;
  owner_user_id: string;
  owner_name: string | null;
  owner_email: string | null;
  total_memories: number;
  memory_limit: number;
  mcp_calls_per_day: number;
  mcp_calls_per_week: number;
}

export interface PlanChangeAuditEntry {
  id: number;
  workspace_id: string;
  workspace_name: string;
  old_plan: string | null;
  new_plan: string;
  /** Actor's stable user_id (OAuth2 sub) — links to /admin/users/{changed_by}. */
  changed_by: string;
  /** Resolved actor display name; null if the user no longer exists. */
  changed_by_name: string | null;
  /** Resolved actor email for display; null if the user no longer exists. */
  changed_by_email: string | null;
  changed_at: string;
  reason: string | null;
}

export interface UpdatePlanRequest {
  plan_name: PlanTier;
  reason?: string;
}

/**
 * List all workspaces with plan info (Admin only)
 */
export async function getAdminWorkspaces(): Promise<WorkspacePlanInfo[]> {
  return apiClient.get<WorkspacePlanInfo[]>("/api/v1/admin/plans/workspaces");
}

/**
 * Update workspace plan tier (Admin only)
 */
export async function updateWorkspacePlan(
  workspaceId: string,
  request: UpdatePlanRequest,
): Promise<{ message: string }> {
  return apiClient.put<{ message: string }>(
    `/api/v1/admin/plans/workspaces/${workspaceId}/plan`,
    request,
  );
}

/**
 * Get plan change audit log (Admin only)
 */
export async function getAdminPlanAudit(
  limit: number = 100,
): Promise<PlanChangeAuditEntry[]> {
  return apiClient.get<PlanChangeAuditEntry[]>(
    `/api/v1/admin/plans/audit?limit=${limit}`,
  );
}

// ============================================================================
// Plan Tiers (Issue #664)
// ============================================================================

export interface PlanTierInfo {
  name: PlanTier;
  display_name: string;
  max_contexts_per_workspace: number;
  max_members_per_workspace: number;
  owned_workspace_grant: number; // #1550: slots the tier grants its owner
  owned_workspaces: number; // #1550: 1 base + grant (what the table shows)
  max_resource_tokens: number;
  max_connectors: number; // #1551: serve-only cap on M/L, creation cap on XL
  memory_limit: number;
  mcp_calls_per_day: number;
  mcp_calls_per_week: number;
  rest_calls_per_day: number;
  rest_calls_per_week: number;
  public_calls_per_day: number;
  public_calls_per_week: number;
  bound_public_calls_per_minute: number;
  analysis_runs_per_day: number;
  storage_limit_bytes: number;
  sleep_enabled_contexts_limit: number;
  // Issue #1549. Optional: an API predating #1549 omits it (rolling deploy);
  // the tiers table renders the gap as "—".
  memories_per_day?: number;
  embedding_daily_cap_usd: number | null; // Issue #709
  embedding_monthly_cap_usd: number | null; // Issue #709
  allows_shared_contexts: boolean;
  // #1551: XL-only "may create" gates as booleans.
  resources: boolean;
  connectors: boolean;
  public_contexts: boolean;
  features: string[];
}

/**
 * BYOK embedding spend cap breakdown for a workspace (Issue #709).
 *
 * - tier_default_*: from PlanTier (server-side env-override aware)
 * - override_*: per-workspace admin override (null = inherit tier default)
 * - effective_*: what the runtime cap check uses (override beats tier default)
 * - current_*: actual BYOK spend so far in the current period (USD)
 */
export interface SpendCapValues {
  tier_default_daily_usd: number | null;
  tier_default_monthly_usd: number | null;
  override_daily_usd: number | null;
  override_monthly_usd: number | null;
  effective_daily_usd: number | null;
  effective_monthly_usd: number | null;
  current_daily_usd: number;
  current_monthly_usd: number;
}

/**
 * Get all plan tier configurations in FREE → BASIC → PRO order (Admin only).
 *
 * Values reflect environment variable overrides applied at backend import
 * time, so the admin tiers tab stays in sync with the true server-side
 * configuration instead of hardcoded i18n numbers.
 */
export async function getAdminPlanTiers(): Promise<PlanTierInfo[]> {
  return apiClient.get<PlanTierInfo[]>("/api/v1/admin/plans/tiers");
}

// ============================================================================
// Workspace Quota Addon Management (Issue #325)
// ============================================================================

export interface QuotaBreakdown {
  memory_limit: number;
  mcp_calls_per_day: number;
  max_contexts: number;
  max_members: number;
  analysis_runs_per_day: number;
  // Issue #665 review fix #8: extended to 9 fields so GET surfaces the
  // effective value for every addon type the PUT accepts. The backend
  // populates these unconditionally (Pydantic ``int = 0`` defaults), so
  // they are typed as required ``number`` not optional — matches the
  // actual wire format and avoids ``undefined`` creeping into UI math
  // (Copilot review feedback on #797).
  rest_calls_per_day: number;
  public_calls_per_day: number;
  storage_bytes_limit: number;
  sleep_enabled_contexts_limit: number;
  // Issue #663: surfaced read-only so the admin panel can show the
  // tier-fixed Resource Token cap alongside the addon-bearing quotas.
  // No corresponding addon column exists — the value is identical
  // across ``base`` and ``effective`` for any given workspace.
  max_resource_tokens: number;
  // Spec 2026-06-02: tier base for the extra_connectors addon.
  max_connectors: number;
  // #1975: tier base for the extra_memories_per_day addon.
  memories_per_day: number;
}

export interface WorkspaceQuotaDetail {
  workspace_id: string;
  workspace_name: string;
  plan_name: string;
  base: QuotaBreakdown;
  // Issue #665: 9 addon cache columns surfaced. The 4 new fields
  // (rest_quota_bonus, public_quota_bonus, storage_bonus_mb,
  // sleep_contexts_bonus) are always present in the response; UI
  // components that don't render them yet (#663 picks that up) simply
  // ignore the extra keys.
  addon: {
    memory_bonus: number;
    mcp_quota_bonus: number;
    rest_quota_bonus: number;
    public_quota_bonus: number;
    member_bonus: number;
    context_bonus: number;
    analysis_bonus: number;
    storage_bonus_mb: number;
    sleep_contexts_bonus: number;
    connector_bonus: number;
    memories_per_day_bonus: number; // #1975
  };
  effective: QuotaBreakdown;
  usage: { memories: number; contexts: number; members: number };
  spend_cap: SpendCapValues | null; // Issue #709
}

export interface UpdateAddonRequest {
  // Issue #665 review-fix #2: all 9 fields are optional with no-touch
  // semantics. Omit a field to leave the corresponding admin grant
  // unchanged; send explicit ``0`` to zero it out. The pre-#665 5-field
  // required-default-0 shape silently DELETEd grants on partial PUTs;
  // unified optional closes that footgun. Existing callers that always
  // send all 5 legacy fields continue to behave identically (explicit
  // values, no omissions).
  //
  // Absolute-value semantics: every concrete value is the desired
  // effective bonus contribution, NOT a delta. Backend rejects values
  // below the active Stripe SUM (HTTP 400) — no silent clamping.
  addon_memory_bonus?: number;
  addon_mcp_quota_bonus?: number;
  addon_member_bonus?: number;
  addon_context_bonus?: number;
  addon_analysis_bonus?: number;
  addon_rest_quota_bonus?: number;
  addon_public_quota_bonus?: number;
  addon_storage_bonus_mb?: number;
  addon_sleep_contexts_bonus?: number;
  addon_connector_bonus?: number;
  addon_memories_per_day_bonus?: number; // #1975
}

/**
 * Update the per-workspace embedding spend cap override (Issue #709).
 *
 * Setting a field to ``null`` removes the override (falls back to tier
 * default). Setting a number (>= 0, and <= tier default) overrides the
 * tier default for this workspace only. The backend rejects values above
 * the tier default — admins lift caps by upgrading the plan.
 */
export interface UpdateSpendCapRequest {
  embedding_daily_cap_usd: number | null;
  embedding_monthly_cap_usd: number | null;
}

/**
 * Get workspace quota details (Admin only)
 */
export async function getWorkspaceQuotas(
  workspaceId: string,
): Promise<WorkspaceQuotaDetail> {
  return apiClient.get<WorkspaceQuotaDetail>(
    `/api/v1/admin/plans/workspaces/${workspaceId}/quotas`,
  );
}

/**
 * Response of the addon PUT (Issue #1561).
 *
 * `warnings` carries structured codes for grants that were stored but are
 * inert on the workspace's current tier — e.g. `connectors_feature_missing`
 * when `extra_connectors` seats are granted below a plan with the
 * `connectors` feature. The backend never rejects such a grant (admins may
 * pre-grant ahead of an upgrade); the UI surfaces the code in the dialog.
 */
export interface UpdateAddonResponse {
  message: string;
  warnings?: string[];
}

/**
 * Update workspace addon bonuses (Admin only)
 */
export async function updateWorkspaceAddons(
  workspaceId: string,
  request: UpdateAddonRequest,
): Promise<UpdateAddonResponse> {
  return apiClient.put<UpdateAddonResponse>(
    `/api/v1/admin/plans/workspaces/${workspaceId}/quotas`,
    request,
  );
}

/**
 * Update the per-workspace embedding spend cap override (Admin only; Issue #709).
 *
 * The backend rejects values above the workspace's current tier default —
 * tier-bounded edit affordance. Pass ``null`` to clear the override and
 * fall back to the tier default.
 */
export async function updateWorkspaceSpendCap(
  workspaceId: string,
  request: UpdateSpendCapRequest,
): Promise<{ message: string }> {
  return apiClient.put<{ message: string }>(
    `/api/v1/admin/plans/workspaces/${workspaceId}/spend-cap`,
    request,
  );
}

// ============================================================================
// User Management (Issue #164, #165)
// ============================================================================

export interface UserWorkspace {
  workspace_id: string;
  workspace_name: string;
  role: WorkspaceRole;
  is_primary: boolean;
  joined_at: string | null;
  plan_name: string;
}

export interface UserContext {
  context_id: string;
  context_name: string;
  workspace_id: string;
  workspace_name: string;
  // Cross-axis: workspace owner/admin bypass returns the WorkspaceRole;
  // explicit ContextMember rows carry the ContextRole. See backend #699.
  role: WorkspaceRole | ContextRole;
  last_used_at: string | null;
}

export interface UserStats {
  total_memories: number;
  working_memories: number;
  persistent_memories: number;
  active_api_keys: number; // Issue #164: Active API keys count
  api_calls_today: number;
  api_calls_week: number;
}

/**
 * One owned workspace in the workspace_summary projection (#676).
 * Distinct from UserWorkspace — no role / joined_at; the admin slot-bonus
 * lens only cares about ownership, not membership granularity.
 */
export interface OwnedWorkspaceInfo {
  id: string;
  name: string;
  plan_name: string;
}

/**
 * Per-user workspace capacity summary for the admin slot bonus UI (#676).
 * base_cap is surfaced explicitly so the frontend does not hardcode the
 * formula — if BASE_CAP ever changes in plan_resolver.py, this just flows
 * through. #1550 adds tier_grant / tier (slots granted by the highest owned
 * tier: cap = base_cap + workspace_slot_bonus + tier_grant); optional during
 * the mixed-version rollout window. is_at_cap is precomputed on the backend
 * so the badge variant does not have to recompute it.
 */
export interface WorkspaceSummary {
  owned_count: number;
  workspace_slot_bonus: number;
  base_cap: number;
  tier_grant?: number;
  tier?: string;
  cap: number;
  is_at_cap: boolean;
  owned_workspaces: OwnedWorkspaceInfo[];
}

export interface UserDetail {
  user: {
    id: string; // Backward compatibility field (same as user_id)
    user_id: string;
    email: string;
    name: string;
    picture: string | null;
    role: string;
    is_initial_admin: boolean;
    created_at: string;
    last_login_at: string | null;
    auth_provider: string | null;
  };
  workspaces: UserWorkspace[];
  accessible_contexts: UserContext[];
  stats: UserStats;
  workspace_summary?: WorkspaceSummary | null; // #676 (optional during rollout)
}

/**
 * Successors for an admin user deletion (#2372): resource id → surviving
 * user_id. A workspace's successor also takes the contexts the user created
 * there unless `context_transfers` names another one.
 */
export interface UserDeleteTransfers {
  workspace_transfers: Record<string, string>;
  context_transfers: Record<string, string>;
}

/**
 * Delete a user (Admin only). Without `transfers` the DELETE has no body; a
 * user who still owns a workspace or created a shared context is then refused
 * with the 409 `userDeleteFailure` reads (#2420).
 */
export async function deleteUser(
  userId: string,
  transfers?: UserDeleteTransfers,
): Promise<unknown> {
  return apiClient.delete<unknown>(
    `/api/v1/admin/users/${encodeURIComponent(userId)}`,
    transfers ? { body: JSON.stringify(transfers) } : {},
  );
}

/**
 * Get user details (Admin only)
 * Issue #164: User detail endpoint
 */
export async function getUserDetails(userId: string): Promise<UserDetail> {
  return apiClient.get<UserDetail>(`/api/v1/admin/users/${userId}`);
}

/**
 * Body for PATCH /admin/users/{user_id}/workspace_slot_bonus (#676).
 * `reason` is server-required only when the delta would create an
 * over-cap state (new_cap < current_owned). The frontend modal mirrors
 * that rule so the admin sees the requirement before submit.
 */
export interface UpdateWorkspaceSlotBonusRequest {
  delta: number;
  reason?: string | null;
}

export interface UpdateWorkspaceSlotBonusResponse {
  before_value: number;
  after_value: number;
  owned_count: number;
  base_cap: number;
  cap: number;
  is_at_cap: boolean;
  reason: string | null;
}

/**
 * Apply a signed delta to a user's workspace_slot_bonus (Admin only).
 *
 * Atomic via UPDATE ... RETURNING on the backend, so two admins clicking
 * +1 simultaneously cannot overwrite each other's update. The response
 * carries enough state for an optimistic UI to reconcile without a refetch.
 *
 * Issue #676.
 */
export async function updateWorkspaceSlotBonus(
  userId: string,
  request: UpdateWorkspaceSlotBonusRequest,
): Promise<UpdateWorkspaceSlotBonusResponse> {
  return apiClient.patch<UpdateWorkspaceSlotBonusResponse>(
    `/api/v1/admin/users/${userId}/workspace_slot_bonus`,
    request,
  );
}
