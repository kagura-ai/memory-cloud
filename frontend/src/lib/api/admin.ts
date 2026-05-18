/**
 * Admin API Client
 *
 * Issue #149: Plan tier enforcement - Admin management
 */

import { apiClient } from "./base";

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
  changed_by: string;
  changed_at: string;
  reason: string | null;
}

export interface UpdatePlanRequest {
  plan_name: "free" | "basic" | "pro";
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
  name: "free" | "basic" | "pro";
  display_name: string;
  price_monthly: number;
  max_contexts_per_workspace: number;
  max_members_per_workspace: number;
  max_resource_tokens: number;
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
  embedding_daily_cap_usd: number | null; // Issue #709
  embedding_monthly_cap_usd: number | null; // Issue #709
  allows_shared_contexts: boolean;
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
}

export interface WorkspaceQuotaDetail {
  workspace_id: string;
  workspace_name: string;
  plan_name: string;
  base: QuotaBreakdown;
  addon: {
    memory_bonus: number;
    mcp_quota_bonus: number;
    member_bonus: number;
    context_bonus: number;
    analysis_bonus: number;
  };
  effective: QuotaBreakdown;
  usage: { memories: number; contexts: number; members: number };
  spend_cap: SpendCapValues | null; // Issue #709
}

export interface UpdateAddonRequest {
  addon_memory_bonus: number;
  addon_mcp_quota_bonus: number;
  addon_member_bonus: number;
  addon_context_bonus: number;
  addon_analysis_bonus: number;
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
 * Update workspace addon bonuses (Admin only)
 */
export async function updateWorkspaceAddons(
  workspaceId: string,
  request: UpdateAddonRequest,
): Promise<{ message: string }> {
  return apiClient.put<{ message: string }>(
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
  role: string;
  is_primary: boolean;
  joined_at: string | null;
  plan_name: string;
}

export interface UserContext {
  context_id: string;
  context_name: string;
  workspace_id: string;
  workspace_name: string;
  role: string;
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
 * through. is_at_cap is precomputed on the backend so the badge variant
 * does not have to recompute it.
 */
export interface WorkspaceSummary {
  owned_count: number;
  workspace_slot_bonus: number;
  base_cap: number;
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
