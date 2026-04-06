/**
 * Usage API Client
 *
 * Issue #48: Usage statistics API types
 */

export interface PlanLimits {
  plan_name: string;
  memory_limit: number;
  // Combined daily sum (MCP + REST + Public).
  daily_api_limit: number;
  // Combined weekly sum (MCP + REST + Public). Issue #198: this now comes
  // from real plan-tier weekly caps instead of the broken `daily * 7` heuristic.
  weekly_api_limit: number;
  // Issue #198: per-tier limits so the dashboard can show the marketed
  // numbers without users wondering why the combined total is higher.
  mcp_daily_limit: number;
  mcp_weekly_limit: number;
  rest_daily_limit: number;
  rest_weekly_limit: number;
  public_daily_limit: number;
  public_weekly_limit: number;
}

export interface CurrentUsage {
  memory_count: number;
  api_calls_today: number;
  api_calls_this_week: number;
  mcp_calls_today: number; // Issue #238: MCP quota separation
  mcp_calls_this_week: number;
  rest_calls_today: number; // Issue #238: REST quota separation
  rest_calls_this_week: number;
  public_calls_today: number; // Issue #238: Public quota separation
  public_calls_this_week: number;
}

export interface UsageStatus {
  current: number;
  limit: number;
  percentage: number;
  is_warning: boolean;
  is_critical: boolean;
  is_exceeded: boolean;
}

export interface UsageCurrentResponse {
  plan: PlanLimits;
  usage: CurrentUsage;
  memory_usage: UsageStatus;
  daily_api_usage: UsageStatus;
  weekly_api_usage: UsageStatus;
}

export interface DailyUsage {
  date: string;
  count: number;
}

export interface UsageHistoryResponse {
  daily_stats: DailyUsage[];
  total_requests: number;
  period_start: string;
  period_end: string;
}

export interface EndpointUsage {
  endpoint: string;
  count: number;
  percentage: number;
}

export interface UsageBreakdownResponse {
  by_endpoint: EndpointUsage[];
  total_requests: number;
  period_days: number;
}
