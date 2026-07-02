/**
 * Workspaces API Client
 *
 * Issue #115 Phase B-5: Workspace-level Multi-tenancy Frontend
 */

import { apiClient } from "./base";
import { WorkspaceRole } from "@/lib/auth/rbac";
import type {
  UsageCurrentResponse,
  UsageHistoryResponse,
  UsageBreakdownResponse,
} from "./usage";

export interface Workspace {
  id: string;
  name: string;
  description: string | null;
  owner_user_id: string;
  plan_name: string;
  member_count: number;
  context_count: number;
  created_at: string;
  current_user_role?: string | null; // Current user's role in this workspace
  /**
   * Memory broadlistening allowlist gate (#497). True iff this workspace
   * is in ANALYSIS_ENABLED_WORKSPACE_IDS on the server. The other 3 gates
   * (Pro tier / BYOK / quota) are evaluated lazily on the actual API call;
   * this flag exists purely so the UI can hide the analyses tab + kebab
   * entry for non-allowlisted workspaces (cleaner UX than showing an
   * empty state).
   */
  analyses_enabled?: boolean;
}

export interface CredentialsStatusInfo {
  api_key_count: number;
  api_key_visible: boolean;
  claude_app_visible: boolean | null;
  chatgpt_app_visible: boolean | null;
  custom_app_count: number;
}

export interface WorkspaceMember {
  user_id: string;
  user_name: string | null;
  user_email: string | null;
  role: WorkspaceRole;
  joined_at: string | null;
  credentials_status?: CredentialsStatusInfo | null; // New: credentials info

  // User activity fields
  last_login_at?: string | null;
  current_context_id?: string | null;
  current_context_name?: string | null;

  // Issue #234: Context access restriction
  allowed_context_ids?: string[] | null;
}

export interface CreateWorkspaceRequest {
  name: string;
  description?: string;
  // Issue #169: Default context settings
  default_context_name?: string;
  default_context_summary?: string;
  default_context_usage_guide?: string;
  default_context_embedding_model?:
    "text-embedding-3-small" | "text-embedding-3-large";
}

export interface UpdateWorkspaceRequest {
  name?: string;
  description?: string;
}

export interface AddMemberRequest {
  user_id: string;
  role: WorkspaceRole;
}

export interface UpdateMemberRoleRequest {
  role: WorkspaceRole;
}

// Issue #249: Context usage statistics
export interface ContextStatsItem {
  context_id: string;
  context_name: string;
  memory_count: number;
  last_activity: string | null;
  member_count: number;
  api_calls_week: number;
  active_users_week: number;
  avg_response_time_ms: number;
}

export interface WorkspaceTotals {
  memory_count: number;
}

// Dashboard context stat (workspace-level, distinct from Qdrant ContextStats in types/context.ts)
export interface DashboardContextStats {
  context_id: string;
  context_name: string;
  created_by: string | null;
  created_by_name: string | null;
  memory_count: number;
  is_private?: boolean;
}

export interface PrivateContextAggregation {
  context_count: number;
  memory_count: number;
}

export interface WorkspaceStats {
  total_memories: number;
  context_count: number;
  contexts: DashboardContextStats[];
  private_aggregation?: PrivateContextAggregation | null;
  plan_name: string;
}

export interface ContextStatsResponse {
  contexts: ContextStatsItem[];
  total_contexts: number;
  workspace_totals: WorkspaceTotals;
}

export interface DailyUsageItem {
  date: string;
  api_calls: number;
  unique_users: number;
}

export interface UserActivityItem {
  user_id: string;
  user_name: string | null;
  user_email: string | null;
  api_calls: number;
  last_activity: string | null;
  timezone?: string;
}

export interface ContextUsageTimelineResponse {
  context_id: string;
  context_name: string;
  daily_usage: DailyUsageItem[];
  total_calls: number;
}

export interface ContextUserActivityResponse {
  context_id: string;
  context_name: string;
  users: UserActivityItem[];
  total_users: number;
}

/**
 * List all workspaces user belongs to
 */
export async function listWorkspaces(): Promise<Workspace[]> {
  return apiClient.get<Workspace[]>("/api/v1/workspaces");
}

/**
 * Create a new workspace
 */
export async function createWorkspace(
  data: CreateWorkspaceRequest,
): Promise<Workspace> {
  return apiClient.post<Workspace>("/api/v1/workspaces", data);
}

/**
 * Get workspace details
 */
export async function getWorkspace(workspaceId: string): Promise<Workspace> {
  return apiClient.get<Workspace>(`/api/v1/workspaces/${workspaceId}`);
}

/**
 * Update workspace
 */
export async function updateWorkspace(
  workspaceId: string,
  data: UpdateWorkspaceRequest,
): Promise<Workspace> {
  return apiClient.put<Workspace>(`/api/v1/workspaces/${workspaceId}`, data);
}

/**
 * Delete workspace
 */
export async function deleteWorkspace(workspaceId: string): Promise<void> {
  return apiClient.delete<void>(`/api/v1/workspaces/${workspaceId}`);
}

/**
 * List workspace members
 */
export async function listMembers(
  workspaceId: string,
): Promise<WorkspaceMember[]> {
  return apiClient.get<WorkspaceMember[]>(
    `/api/v1/workspaces/${workspaceId}/members`,
  );
}

/**
 * Add member to workspace
 */
export async function addMember(
  workspaceId: string,
  data: AddMemberRequest,
): Promise<WorkspaceMember> {
  return apiClient.post<WorkspaceMember>(
    `/api/v1/workspaces/${workspaceId}/members`,
    data,
  );
}

/**
 * Update member role
 */
export async function updateMemberRole(
  workspaceId: string,
  userId: string,
  data: UpdateMemberRoleRequest,
): Promise<WorkspaceMember> {
  return apiClient.put<WorkspaceMember>(
    `/api/v1/workspaces/${workspaceId}/members/${userId}`,
    data,
  );
}

/**
 * Remove member from workspace
 */
export async function removeMember(
  workspaceId: string,
  userId: string,
): Promise<void> {
  return apiClient.delete<void>(
    `/api/v1/workspaces/${workspaceId}/members/${userId}`,
  );
}

/**
 * Update member's context access restriction
 * Issue #234: Context access restriction for member/viewer
 *
 * @param allowedContextIds - null: no restriction, []: no access, [uuid,...]: only these contexts
 */
export async function updateMemberContextAccess(
  workspaceId: string,
  userId: string,
  allowedContextIds: string[] | null,
): Promise<{
  status: string;
  user_id: string;
  allowed_context_ids: string[] | null;
}> {
  return apiClient.put(
    `/api/v1/workspaces/${workspaceId}/members/${userId}/context-access`,
    {
      allowed_context_ids: allowedContextIds,
    },
  );
}

/**
 * Get workspace statistics
 */
export async function getWorkspaceStats(workspaceId: string): Promise<{
  total_memories: number;
  total_storage_mb: number;
  context_count: number;
  member_count: number;
}> {
  return apiClient.get(`/api/v1/workspaces/${workspaceId}/stats`);
}

/**
 * Switch to a different workspace
 */
export async function switchWorkspace(workspaceId: string): Promise<void> {
  return apiClient.put<void>(`/api/v1/workspaces/${workspaceId}/switch`);
}

/**
 * Get workspace-wide current usage vs plan limits
 * Aggregates usage across all workspace members
 */
export async function getWorkspaceUsageCurrent(): Promise<UsageCurrentResponse> {
  return apiClient.get<UsageCurrentResponse>("/api/v1/workspace/usage/current");
}

/**
 * Get workspace-wide historical usage data
 * Aggregates daily API calls across all workspace members
 */
export async function getWorkspaceUsageHistory(
  days: number = 7,
): Promise<UsageHistoryResponse> {
  return apiClient.get<UsageHistoryResponse>(
    `/api/v1/workspace/usage/history?days=${days}`,
  );
}

/**
 * Get workspace-wide usage breakdown by endpoint
 * Aggregates endpoint usage across all workspace members
 */
export async function getWorkspaceUsageBreakdown(
  days: number = 30,
): Promise<UsageBreakdownResponse> {
  return apiClient.get<UsageBreakdownResponse>(
    `/api/v1/workspace/usage/breakdown?days=${days}`,
  );
}

// ============================================================================
// Workspace Plan Management (Issue #164)
// ============================================================================

export interface WorkspacePlanInfo {
  workspace_id: string;
  workspace_name: string;
  current_plan: string;
  plan_display_name: string;
  price_monthly: number;
  usage: {
    memories: number;
    contexts: number;
  };
  quotas: {
    memory_limit: number;
    max_contexts: number;
    mcp_calls_per_day: number;
    mcp_calls_per_week: number;
    rest_calls_per_day: number;
    public_calls_per_day: number;
  };
  can_upgrade: boolean;
  can_downgrade: boolean;
}

export interface AvailablePlanInfo {
  name: string;
  display_name: string;
  price_monthly: number;
  quotas: {
    memory_limit: number;
    max_contexts: number;
    mcp_calls_per_day: number;
    mcp_calls_per_week: number;
    rest_calls_per_day: number;
    public_calls_per_day: number;
  };
  features: string[];
}

/**
 * Get workspace plan information
 * Issue #164: Workspace plan management
 */
export async function getWorkspacePlan(
  workspaceId: string,
): Promise<WorkspacePlanInfo> {
  return apiClient.get<WorkspacePlanInfo>(
    `/api/v1/workspaces/${workspaceId}/plan`,
  );
}

/**
 * One tier's curated feature/limit values for the Plan-page comparison matrix
 * (#1138). Mirrors the backend `PlanTierFeature`. Numeric fields use `0` for
 * "not available on this tier"; price is intentionally absent (it lives on the
 * payment side — #1141 / #1096).
 */
export interface PlanTierFeature {
  name: string;
  display_name: string;
  max_contexts: number;
  max_members: number;
  memory_limit: number;
  storage_limit_bytes: number;
  mcp_calls_per_day: number;
  rest_calls_per_day: number;
  public_calls_per_day: number;
  max_resource_tokens: number;
  max_connectors: number;
  analysis_runs_per_day: number;
  sleep_enabled_contexts_limit: number;
  reranking: boolean;
  managed_embeddings: boolean;
  secret_store: boolean;
  shared_contexts: boolean;
  team_invitations: boolean;
}

/** Curated per-tier feature matrix (free → basic → pro) for the Plan page (#1138). */
export async function getPlanTierMatrix(): Promise<PlanTierFeature[]> {
  return apiClient.get<PlanTierFeature[]>("/api/v1/workspaces/plans/tiers");
}

/**
 * Get available plan tiers
 * Issue #164: Plan selection
 */
export async function getAvailablePlans(): Promise<AvailablePlanInfo[]> {
  return apiClient.get<AvailablePlanInfo[]>(
    "/api/v1/workspaces/plans/available",
  );
}

/**
 * Get context usage statistics for workspace
 * Issue #249: Context usage overview
 */
export async function getContextStats(
  workspaceId: string,
): Promise<ContextStatsResponse> {
  return apiClient.get<ContextStatsResponse>(
    `/api/v1/workspaces/${workspaceId}/contexts/stats`,
  );
}

/**
 * Get usage timeline for a specific context
 * Issue #249: Time-series usage data
 */
export async function getContextUsageTimeline(
  workspaceId: string,
  contextId: string,
  days: number = 7,
): Promise<ContextUsageTimelineResponse> {
  return apiClient.get<ContextUsageTimelineResponse>(
    `/api/v1/workspaces/${workspaceId}/contexts/${contextId}/usage-timeline?days=${days}`,
  );
}

/**
 * Get user activity for a specific context
 * Issue #249: Per-user activity breakdown
 */
export async function getContextUserActivity(
  workspaceId: string,
  contextId: string,
  days: number = 7,
): Promise<ContextUserActivityResponse> {
  return apiClient.get<ContextUserActivityResponse>(
    `/api/v1/workspaces/${workspaceId}/contexts/${contextId}/user-activity?days=${days}`,
  );
}

/**
 * Memory Timeline (Issue #275 Task 6)
 */

export interface DailyMemoryCount {
  date: string;
  count: number;
}

export interface MemoryTimelineResponse {
  workspace_id: string;
  workspace_name: string;
  daily_counts: DailyMemoryCount[];
  memories_created_in_period: number; // Renamed for clarity
  period_start: string;
  period_end: string;
}

export async function getWorkspaceMemoryTimeline(
  workspaceId: string,
  days: number = 30,
  contextId?: string,
): Promise<MemoryTimelineResponse> {
  const params = new URLSearchParams({ days: days.toString() });
  if (contextId) params.set("context_id", contextId);
  return apiClient.get<MemoryTimelineResponse>(
    `/api/v1/workspaces/${workspaceId}/memory-timeline?${params.toString()}`,
  );
}

/**
 * Resource Ingest API statistics
 * Issue #265
 */
export interface ResourceIngestStats {
  total_events: number;
  last_n_days: number;
  avg_per_day: number;
  active_tokens: number;
  timeline: Array<{ date: string; count: number }>;
}

/**
 * Public Search API statistics
 * Issue #265
 */
export interface PublicSearchStats {
  total_searches: number;
  last_n_days: number;
  anonymous: number;
  authenticated: number;
  timeline: Array<{
    date: string;
    total: number;
    anonymous: number;
    authenticated: number;
  }>;
}

/**
 * Public API statistics response
 * Issue #265
 */
export interface PublicAPIStatsResponse {
  resource_ingest: ResourceIngestStats;
  public_search: PublicSearchStats;
}

/**
 * Get Public API usage statistics for a public context
 * Issue #265: Resource Ingest and Public Search API stats
 */
export async function getContextPublicAPIStats(
  workspaceId: string,
  contextId: string,
  days: number = 7,
): Promise<PublicAPIStatsResponse> {
  return apiClient.get<PublicAPIStatsResponse>(
    `/api/v1/workspaces/${workspaceId}/contexts/${contextId}/public-api-stats?days=${days}`,
  );
}

/**
 * OpenAI API key status for workspace
 * Issue #181: API key guidance in context creation
 */
export interface OpenAIKeyStatus {
  has_key: boolean;
  can_configure: boolean;
  external_keys_url: string;
}

/**
 * Check if workspace has OpenAI API key
 * Issue #181: Prevent context creation errors
 */
export async function checkOpenAIKeyStatus(
  workspaceId: string,
): Promise<OpenAIKeyStatus> {
  return apiClient.get<OpenAIKeyStatus>(
    `/api/v1/workspaces/${workspaceId}/openai-key-status`,
  );
}

// ============================================================================
// Member Usage (Issue #331)
// ============================================================================

export interface MemberUsageEntry {
  user_id: string;
  name: string | null;
  email: string | null;
  memory_count: number;
  api_calls_today: number;
  api_calls_week: number;
}

export interface MemberUsageResponse {
  members: MemberUsageEntry[];
  total_members: number;
}

export async function getWorkspaceMemberUsage(): Promise<MemberUsageResponse> {
  return apiClient.get<MemberUsageResponse>("/api/v1/workspace/usage/members");
}
