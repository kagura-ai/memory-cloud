/**
 * Sleep Reports API Client
 *
 * Issue #526: workspace-scoped sleep reports view.
 * Wraps the two endpoints:
 * - GET /api/v1/admin/sleep-reports (admin cross-workspace)
 * - GET /api/v1/workspaces/{workspace_id}/sleep-reports (owner/admin scoped)
 *
 * Both endpoints accept the same status / limit / offset / user_id /
 * context_id filters; the workspace-scoped one omits ``workspace_id``
 * from the query string because it comes from the path.
 */

import { apiClient } from "./base";
import { SLEEP_STATUS_OPTIONS, type SleepStatus } from "@/lib/sleep-report";

export { SLEEP_STATUS_OPTIONS, type SleepStatus };

export interface SleepReportSummary {
  id: string;
  user_id: string;
  workspace_id: string | null;
  context_id: string | null;
  context_name: string | null;
  status: SleepStatus;
  started_at: string;
  completed_at: string | null;
  memories_processed: number;
  edges_created: number;
  memories_merged: number;
  memories_promoted: number;
  memories_flagged: number;
  llm_calls_made: number;
  llm_tokens_used: number;
  // #1183: judge-LLM calls that raised across all phases — the magnitude
  // behind a 'degraded'/'failed' grading. Optional: reports written before
  // v0.43.0 lack the column in older cached payloads.
  llm_call_failures?: number;
}

export interface PhaseResult {
  success: boolean;
  skipped: boolean;
  skip_reason: string | null;
  error: string | null;
  llm_calls: number;
  // #1183: judge calls that raised in THIS phase. Absent on pre-v0.43.0
  // report blobs (JSONB written by older reporters).
  llm_call_failures?: number;
  memories_processed: number;
  details: Record<string, unknown> | null;
}

export interface SleepReportDetail extends SleepReportSummary {
  context_deleted: boolean;
  embedding_calls_made: number;
  error_message: string | null;
  edge_discovery_result: PhaseResult | null;
  dedup_result: PhaseResult | null;
  importance_result: PhaseResult | null;
  consolidation_result: PhaseResult | null;
  reindex_result: PhaseResult | null;
}

export interface SleepActionItem {
  id: number;
  phase: string;
  action_type: string;
  memory_id: string | null;
  target_id: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

export interface SleepReportListResponse {
  reports: SleepReportSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface SleepReportDetailResponse {
  report: SleepReportDetail;
  actions: SleepActionItem[];
  action_count: number;
}

export interface SleepRunResponse {
  report_ids: string[];
}

export interface FetchAdminSleepReportsParams {
  status?: SleepStatus;
  limit?: number;
  offset?: number;
  user_id?: string;
  context_id?: string;
}

export interface FetchWorkspaceSleepReportsParams {
  status?: SleepStatus;
  limit?: number;
  offset?: number;
  user_id?: string;
  context_id?: string;
}

/**
 * Fetch admin (cross-workspace) sleep reports list.
 */
export async function fetchAdminSleepReports(
  params: FetchAdminSleepReportsParams = {},
): Promise<SleepReportListResponse> {
  const search = new URLSearchParams();
  if (params.status) search.set("status", params.status);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  if (params.user_id) search.set("user_id", params.user_id);
  if (params.context_id) search.set("context_id", params.context_id);
  const query = search.toString();
  return apiClient.get<SleepReportListResponse>(
    `/api/v1/admin/sleep-reports${query ? `?${query}` : ""}`,
  );
}

/**
 * Fetch a single admin sleep report detail.
 */
export async function fetchAdminSleepReportDetail(
  reportId: string,
): Promise<SleepReportDetailResponse> {
  return apiClient.get<SleepReportDetailResponse>(
    `/api/v1/admin/sleep-reports/${encodeURIComponent(reportId)}`,
  );
}

/**
 * Fetch workspace-scoped sleep reports list.
 */
export async function fetchWorkspaceSleepReports(
  workspaceId: string,
  params: FetchWorkspaceSleepReportsParams = {},
): Promise<SleepReportListResponse> {
  const search = new URLSearchParams();
  if (params.status) search.set("status", params.status);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  if (params.user_id) search.set("user_id", params.user_id);
  if (params.context_id) search.set("context_id", params.context_id);
  const query = search.toString();
  return apiClient.get<SleepReportListResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/sleep-reports${query ? `?${query}` : ""}`,
  );
}

/**
 * Fetch a single workspace-scoped sleep report detail.
 */
export async function fetchWorkspaceSleepReportDetail(
  workspaceId: string,
  reportId: string,
): Promise<SleepReportDetailResponse> {
  return apiClient.get<SleepReportDetailResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/sleep-reports/${encodeURIComponent(reportId)}`,
  );
}
