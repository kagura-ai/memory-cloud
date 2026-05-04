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
}

export interface UpdateAddonRequest {
  addon_memory_bonus: number;
  addon_mcp_quota_bonus: number;
  addon_member_bonus: number;
  addon_context_bonus: number;
  addon_analysis_bonus: number;
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
}

/**
 * Get user details (Admin only)
 * Issue #164: User detail endpoint
 */
export async function getUserDetails(userId: string): Promise<UserDetail> {
  return apiClient.get<UserDetail>(`/api/v1/admin/users/${userId}`);
}
