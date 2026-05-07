/**
 * Usage API Client
 *
 * Issue #48: Usage statistics API types
 */

export interface PlanLimits {
  plan_name: string;
  memory_limit: number;
  // Issue #147: Combined daily/weekly sums (MCP + REST + Public).
  daily_total_limit: number;
  weekly_total_limit: number;
  // Issue #147: Per-type limits (aligned with internal naming convention).
  mcp_calls_per_day: number;
  mcp_calls_per_week: number;
  rest_calls_per_day: number;
  rest_calls_per_week: number;
  public_calls_per_day: number;
  public_calls_per_week: number;
}

/**
 * Sleep-enabled contexts quota (Issue #560).
 *
 * Mirrors the 429 quota-exceeded body so the dashboard, the SettingsTabPanel
 * disabled-state, and the gate rejection all read the same field names.
 */
export interface SleepContextsUsage {
  used: number;
  limit: number;
  addon_bonus: number;
  remaining: number;
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
  sleep_contexts: SleepContextsUsage | null; // Issue #560
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
