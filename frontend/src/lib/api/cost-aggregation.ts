/**
 * Cost Aggregation API Client
 *
 * Issue #473: frontend cost dashboard.
 * Wraps the two endpoints from #472:
 * - GET /api/v1/admin/cost-aggregation (admin cross-workspace)
 * - GET /api/v1/workspaces/{workspace_id}/cost-aggregation (owner/admin scoped)
 *
 * Both endpoints are now consumed:
 * - ``fetchAdminCostAggregation`` powers ``/admin/cost`` (admin-only,
 *   cross-workspace view).
 * - ``fetchWorkspaceCostAggregation`` powers ``/workspace/cost``
 *   (workspace owner/admin self-service, single workspace).
 */

import { apiClient } from "./base";

/**
 * Allowed period values. Mirrors backend ``VALID_PERIODS`` —
 * out-of-range values raise 400 at the route layer.
 */
export const COST_AGGREGATION_PERIODS = ["day", "week", "month"] as const;
export type CostAggregationPeriod = (typeof COST_AGGREGATION_PERIODS)[number];

/**
 * Source classification for a cost row. Mirrors the DB enum on
 * ``sleep_reports.source`` (#523): scheduler-driven sleep runs vs
 * on-demand Memory Analysis.
 */
export const COST_SOURCES = ["sleep", "analysis"] as const;
export type CostSource = (typeof COST_SOURCES)[number];

/**
 * Billing classification. ``platform`` rows contribute to ``cost_usd``
 * (B2B billing); ``byok`` rows contribute to ``cost_usd_byok``
 * (observability-only — workspace paid the provider directly).
 */
export const COST_PAID_BY_VALUES = ["platform", "byok"] as const;
export type CostPaidBy = (typeof COST_PAID_BY_VALUES)[number];

/**
 * Per-model cost breakdown row inside a CostAggregationRow.
 *
 * ``cost_usd`` / ``cost_usd_byok`` are nullable — ``null`` means
 * "cost unknown" (some contributing usage row had no resolved
 * pricing, e.g. a model with no row in ``llm_pricing`` at the run's
 * ``started_at``). Render NULL as "—" in the UI to distinguish from
 * a genuine $0 cost.
 */
export interface CostBreakdownByModel {
  model: string | null;
  calls: number;
  cost_usd: number | null;
  cost_usd_byok: number | null;
}

/**
 * Per-source cost breakdown row inside a CostAggregationRow.
 * Same nullable-cost semantics as ``CostBreakdownByModel``.
 */
export interface CostBreakdownBySource {
  source: CostSource;
  calls: number;
  cost_usd: number | null;
  cost_usd_byok: number | null;
}

/**
 * One (period × workspace × user) aggregation row.
 *
 * Both cost fields are nullable per the sticky-NULL rule: if any
 * contributing usage row in this bucket lacked pricing, the bucket's
 * cost is ``null``. The dashboard renders null as "—" with a tooltip
 * so it doesn't look like the workspace spent $0 in a period that
 * actually contained unpriced usage.
 */
export interface CostAggregationRow {
  /** ISO date string (YYYY-MM-DD) at the period bucket start. */
  period_start: string;
  workspace_id: string | null;
  user_id: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  tokens_cached_in: number;
  embedding_tokens: number;
  cost_usd: number | null;
  cost_usd_byok: number | null;
  cost_breakdown_by_model: CostBreakdownByModel[];
  cost_breakdown_by_source: CostBreakdownBySource[];
}

export interface CostAggregationResponse {
  rows: CostAggregationRow[];
}

/**
 * Filter parameters accepted by the admin cost aggregation endpoint.
 * ``from`` / ``to`` are required ISO dates (YYYY-MM-DD); the rest are
 * optional. Server-side validation rejects unknown ``source`` /
 * ``paid_by`` values with 400.
 */
export interface FetchAdminCostAggregationParams {
  /** Aggregation granularity. Defaults to ``"day"`` server-side. */
  period?: CostAggregationPeriod;
  /** Inclusive lower-bound date (YYYY-MM-DD). */
  from: string;
  /** Inclusive upper-bound date (YYYY-MM-DD). */
  to: string;
  /** Filter to a single workspace (optional). */
  workspace_id?: string;
  /** Filter to a single user (optional). */
  user_id?: string;
  /** Filter by source classification (optional). */
  source?: CostSource;
  /** Filter by billing classification (optional). */
  paid_by?: CostPaidBy;
}

/**
 * Fetch the admin (cross-workspace) cost aggregation.
 *
 * Caller must be a system admin — non-admin requests get 403 from the
 * route's ``require_admin`` dependency. Anonymous requests get 401.
 *
 * @returns ``CostAggregationResponse`` with rows sorted by
 *          (period_start, workspace_id, user_id).
 * @throws ``ApiError`` on 400/401/403/422/5xx with the server's
 *         ``detail`` string.
 */
export async function fetchAdminCostAggregation(
  params: FetchAdminCostAggregationParams,
): Promise<CostAggregationResponse> {
  const search = new URLSearchParams();
  if (params.period) search.set("period", params.period);
  search.set("from", params.from);
  search.set("to", params.to);
  if (params.workspace_id) search.set("workspace_id", params.workspace_id);
  if (params.user_id) search.set("user_id", params.user_id);
  if (params.source) search.set("source", params.source);
  if (params.paid_by) search.set("paid_by", params.paid_by);

  return apiClient.get<CostAggregationResponse>(
    `/api/v1/admin/cost-aggregation?${search.toString()}`,
  );
}

/**
 * Filter parameters for the workspace-scoped cost aggregation endpoint.
 * The workspace is path-bound (passed separately to the fetch function),
 * so it is NOT in the params object — preventing accidental cross-workspace
 * probes via query string.
 */
export type FetchWorkspaceCostAggregationParams = Omit<
  FetchAdminCostAggregationParams,
  "workspace_id"
>;

/**
 * Fetch cost aggregation for a single workspace.
 *
 * Caller must be a workspace **owner** or **admin** — viewer / member
 * are rejected by the backend's ``check_workspace_admin`` gate (cost
 * aggregates leak per-user activity volume across private contexts that
 * lower roles should not see).
 *
 * The workspace_id is path-bound; passing one via the params type is
 * structurally prevented.
 */
export async function fetchWorkspaceCostAggregation(
  workspaceId: string,
  params: FetchWorkspaceCostAggregationParams,
): Promise<CostAggregationResponse> {
  const search = new URLSearchParams();
  if (params.period) search.set("period", params.period);
  search.set("from", params.from);
  search.set("to", params.to);
  if (params.user_id) search.set("user_id", params.user_id);
  if (params.source) search.set("source", params.source);
  if (params.paid_by) search.set("paid_by", params.paid_by);

  return apiClient.get<CostAggregationResponse>(
    `/api/v1/workspaces/${encodeURIComponent(workspaceId)}/cost-aggregation?${search.toString()}`,
  );
}
