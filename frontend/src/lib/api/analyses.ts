/**
 * Memory Broadlistening Analyses API Client (Issue #497)
 *
 * Wraps the REST surface from #496 plus the cluster + position
 * endpoints added in #497 (extending the run-level reads with the
 * cluster-level data the frontend needs to render the scatter view).
 */

import { apiClient } from "./base";

// ============================================================================
// Constants (mirror backend enums)
// ============================================================================

export const ANALYSIS_STATUSES = [
  "running",
  "succeeded",
  "failed",
  "cancelled",
] as const;
export type AnalysisStatus = (typeof ANALYSIS_STATUSES)[number];

// ============================================================================
// Types
// ============================================================================

/**
 * One analysis run row. Mirrors the backend ``AnalysisRow`` model.
 *
 * Cost fields (``cost_estimated_cents`` / ``cost_actual_cents``) are
 * nullable — render NULL as "—" via ``formatCostCents`` so an unpriced
 * run does not visually equal a $0.00 run.
 */
export interface AnalysisRunRow {
  run_id: string;
  workspace_id: string;
  context_id: string;
  status: AnalysisStatus;
  triggered_by: string;
  started_at: string;
  finished_at: string | null;
  input_count: number;
  cost_estimated_cents: number | null;
  cost_actual_cents: number | null;
  error: string | null;
  cancellation_reason: string | null;
}

export interface AnalysisListResponse {
  items: AnalysisRunRow[];
  next_cursor: string | null;
}

export interface AnalysisStartResponse {
  run_id: string;
  status: AnalysisStatus;
  started_at: string;
}

export interface AnalysisCancelResponse {
  run_id: string;
  status: AnalysisStatus;
  cancellation_reason: string | null;
  finished_at: string | null;
}

export interface AnalysisPreviewResponse {
  memory_count: number;
  cluster_count_estimate: number;
  estimated_cost_cents: number;
  model_id: string;
  breakdown: Record<string, number>;
}

/**
 * One cluster within an analysis run. Mirrors backend ``ClusterRow``.
 *
 * ``representative_memory_ids`` is the raw UUID list as stored on the
 * cluster row. Resolve to memory summaries via a separate
 * ``recall(filters={"id": [...]})`` call rather than embedding here.
 *
 * ``centroid_2d`` is ``[x, y]`` in the same UMAP coordinate space as
 * ``ScatterPosition.x/y`` returned from ``listRunPositions``.
 */
export interface AnalysisCluster {
  cluster_index: number;
  label: string;
  description: string | null;
  count: number;
  centroid_2d: [number, number];
  representative_memory_ids: string[];
  property_stats: Record<string, unknown>;
  label_confidence: number;
}

export interface AnalysisClusterListResponse {
  items: AnalysisCluster[];
}

/**
 * One ``(memory_id, x, y, cluster_index)`` row for the scatter plot.
 * Coordinates are in the run's UMAP 2D space — render scaled to the
 * scatter viewport via min/max normalization on the client.
 */
export interface ScatterPosition {
  memory_id: string;
  x: number;
  y: number;
  cluster_index: number;
}

export interface AnalysisPositionListResponse {
  items: ScatterPosition[];
}

// ============================================================================
// Request bodies
// ============================================================================

/**
 * Filter parameters used by both ``previewAnalysis`` and ``startAnalysis``.
 * The backend accepts the same shape for both (intentional — the modal
 * submits one body twice: once to preview, once to confirm).
 *
 * ``from`` / ``to`` are ISO-8601 date strings (YYYY-MM-DD form for
 * `<input type="date">`, or full datetime). Use ``formatLocalDate``
 * from ``lib/utils/datetime`` to derive the date from a ``Date``
 * instance without UTC-shifting (the cost dashboard uses the same
 * helper).
 */
export interface AnalysisFilters {
  from?: string;
  to?: string;
  types?: string[];
  tags?: string[];
  min_importance?: number;
  query?: string;
  model_id?: number;
}

// ============================================================================
// Endpoints
// ============================================================================

const base = (contextId: string) =>
  `/api/v1/contexts/${encodeURIComponent(contextId)}/analyses`;

/**
 * Pre-flight cost estimate. No row is created. Goes through the
 * full 4-stage gate so a non-Pro / quota-exhausted caller receives
 * 403/429 here rather than after typing the modal form.
 */
export async function previewAnalysis(
  contextId: string,
  body: AnalysisFilters,
): Promise<AnalysisPreviewResponse> {
  return apiClient.post<AnalysisPreviewResponse>(
    `${base(contextId)}/preview`,
    body,
  );
}

/**
 * Kick off a background analysis run. Returns 202 with the new
 * ``run_id``. Poll ``getAnalysisRun`` for status transitions.
 *
 * 409 (ConflictError) → a prior run for the same (workspace, context)
 * is still running. The error body carries the existing ``run_id`` so
 * the client can switch into "watching that run" mode.
 */
export async function startAnalysis(
  contextId: string,
  body: AnalysisFilters,
): Promise<AnalysisStartResponse> {
  return apiClient.post<AnalysisStartResponse>(base(contextId), body);
}

export interface ListAnalysisRunsParams {
  cursor?: string;
  limit?: number;
}

export async function listAnalysisRuns(
  contextId: string,
  params: ListAnalysisRunsParams = {},
): Promise<AnalysisListResponse> {
  const search = new URLSearchParams();
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  const query = search.toString();
  return apiClient.get<AnalysisListResponse>(
    query ? `${base(contextId)}?${query}` : base(contextId),
  );
}

/**
 * Most recent succeeded run for a context. 404 when none exists yet.
 */
export async function getActiveAnalysis(
  contextId: string,
): Promise<AnalysisRunRow> {
  return apiClient.get<AnalysisRunRow>(`${base(contextId)}/active`);
}

export async function getAnalysisRun(
  contextId: string,
  runId: string,
): Promise<AnalysisRunRow> {
  return apiClient.get<AnalysisRunRow>(
    `${base(contextId)}/${encodeURIComponent(runId)}`,
  );
}

/**
 * Soft-cancel a running run. Idempotent on terminal runs (200 with
 * the current state). Cancellation does NOT decrement the daily quota.
 */
export async function cancelAnalysisRun(
  contextId: string,
  runId: string,
): Promise<AnalysisCancelResponse> {
  return apiClient.delete<AnalysisCancelResponse>(
    `${base(contextId)}/${encodeURIComponent(runId)}`,
  );
}

/**
 * List all clusters for a run. No pagination — bounded by
 * ``ceil(sqrt(memory_count))``. Empty array when the run is still
 * running or did not reach the labeler stage.
 */
export async function listRunClusters(
  contextId: string,
  runId: string,
): Promise<AnalysisClusterListResponse> {
  return apiClient.get<AnalysisClusterListResponse>(
    `${base(contextId)}/${encodeURIComponent(runId)}/clusters`,
  );
}

/**
 * List all per-memory 2D positions for the scatter plot. Bounded by
 * the run's ``input_count``. Empty array when no assignments yet.
 */
export async function listRunPositions(
  contextId: string,
  runId: string,
): Promise<AnalysisPositionListResponse> {
  return apiClient.get<AnalysisPositionListResponse>(
    `${base(contextId)}/${encodeURIComponent(runId)}/positions`,
  );
}
