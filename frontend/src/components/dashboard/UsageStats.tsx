"use client";

/**
 * Usage Statistics Component
 *
 * Shows plan limits, current usage, and usage trends.
 * Issue #48 - Usage Statistics - Plan Limits & Usage Tracking
 * Issue #223 - i18n support
 */

import {
  useEffect,
  useMemo,
  useState,
  forwardRef,
  useImperativeHandle,
} from "react";
import { useTranslations, useLocale } from "next-intl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Progress } from "@/components/ui/progress";
import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  CartesianGrid,
} from "recharts";
import { Brain, Zap, TrendingUp, XCircle, Moon, Briefcase } from "lucide-react";
import { apiClient } from "@/lib/api";
import type {
  PlanLimits,
  SleepContextsUsage,
  WorkspacesUsage,
} from "@/lib/api/usage";
import {
  getWorkspaceUsageCurrent,
  getWorkspaceUsageHistory,
  getWorkspaceUsageBreakdown,
} from "@/lib/api/workspaces";
import { useMemoryContext } from "@/contexts/MemoryContextContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useAuth } from "@/contexts/AuthContext";
import { formatDate } from "@/lib/utils/datetime";
import { QuotaWarning } from "@/components/common/QuotaWarning";

interface CurrentUsage {
  memory_count: number;
  api_calls_today: number;
  api_calls_this_week: number;
  mcp_calls_today: number; // Issue #238
  mcp_calls_this_week: number; // Issue #238
  rest_calls_today: number; // Issue #238
  rest_calls_this_week: number; // Issue #238
  public_calls_today: number; // Issue #238
  public_calls_this_week: number; // Issue #238
  sleep_contexts: SleepContextsUsage | null; // Issue #560
  workspaces: WorkspacesUsage | null; // Issue #661
}

interface UsageStatus {
  current: number;
  limit: number;
  percentage: number;
  is_warning: boolean;
  is_critical: boolean;
  is_exceeded: boolean;
}

interface UsageCurrentData {
  plan: PlanLimits;
  usage: CurrentUsage;
  memory_usage: UsageStatus;
  daily_api_usage: UsageStatus;
  weekly_api_usage: UsageStatus;
}

interface DailyUsage {
  date: string;
  count: number;
}

interface UsageHistory {
  daily_stats: DailyUsage[];
  total_requests: number;
  period_start: string;
  period_end: string;
}

interface EndpointUsage {
  endpoint: string;
  count: number;
  percentage: number;
}

interface UsageBreakdown {
  by_endpoint: EndpointUsage[];
  total_requests: number;
  period_days: number;
}

// Tailwind background classes for the per-endpoint color chips next to
// each list row. Index-based cycling keeps the row chips visually
// distinct without inline `style={{ backgroundColor }}` (which would
// violate the "no inline styles" rule in .claude/rules/frontend.md).
// Tailwind's JIT only retains classes that appear as static strings
// somewhere in the source — the literal strings here are what makes
// that work; do not switch to dynamic concatenation like
// `bg-${color}-500`.
const SWATCH_BG_CLASSES = [
  "bg-emerald-500",
  "bg-blue-500",
  "bg-violet-500",
  "bg-amber-500",
  "bg-red-500",
  "bg-cyan-500",
];

export interface UsageStatsRef {
  refresh: () => Promise<void>;
}

export interface UsageStatsProps {
  scope?: "user" | "workspace"; // Default: 'user' (backward compatible)
  className?: string;
}

export const UsageStats = forwardRef<UsageStatsRef, UsageStatsProps>(
  ({ scope = "user", className }, ref) => {
    const t = useTranslations("usageStats");

    const { contextId } = useMemoryContext(); // For user-scoped (context)
    const { currentWorkspaceId } = useWorkspace(); // For workspace-scoped
    const { user } = useAuth();
    const locale = useLocale();
    const [currentUsage, setCurrentUsage] = useState<UsageCurrentData | null>(
      null,
    );
    const [history, setHistory] = useState<UsageHistory | null>(null);
    const [breakdown, setBreakdown] = useState<UsageBreakdown | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // Top-10 + "Other" rollup for the endpoint distribution. Showing
    // every endpoint produces a 25+-row list dominated by 0.0% entries
    // that crowd out the signal. The rollup keeps the list percentages
    // adding to 100% so the UI doesn't lie about scale.
    const TOP_ENDPOINTS_LIMIT = 10;
    const topEndpoints = useMemo<EndpointUsage[]>(() => {
      if (!breakdown) return [];
      const sorted = [...breakdown.by_endpoint].sort(
        (a, b) => b.count - a.count,
      );
      if (sorted.length <= TOP_ENDPOINTS_LIMIT) return sorted;
      const top = sorted.slice(0, TOP_ENDPOINTS_LIMIT);
      const rest = sorted.slice(TOP_ENDPOINTS_LIMIT);
      const otherCount = rest.reduce((sum, ep) => sum + ep.count, 0);
      // Compute Other's share as the residual rather than summing the
      // backend's pre-rounded per-endpoint percentages — that path
      // accumulates rounding error across 15+ entries and drifts off
      // 100%. Clamp to ≥0 in case the top-10 sum itself overflows
      // 100% from the same artifact.
      const topPercentageSum = top.reduce((sum, ep) => sum + ep.percentage, 0);
      const otherPercentage = Math.max(0, 100 - topPercentageSum);
      return [
        ...top,
        {
          endpoint: t("otherEndpointsLabel", { count: rest.length }),
          count: otherCount,
          percentage: otherPercentage,
        },
      ];
    }, [breakdown, t]);

    const fetchAllStats = async () => {
      try {
        setIsLoading(true);

        // Use different API endpoints based on scope
        const [current, hist, brkdwn] =
          scope === "workspace"
            ? await Promise.all([
                getWorkspaceUsageCurrent(),
                getWorkspaceUsageHistory(7),
                getWorkspaceUsageBreakdown(30),
              ])
            : await Promise.all([
                apiClient.get<UsageCurrentData>("/api/v1/usage/current"),
                apiClient.get<UsageHistory>("/api/v1/usage/history?days=7"),
                apiClient.get<UsageBreakdown>(
                  "/api/v1/usage/breakdown?days=30",
                ),
              ]);

        setCurrentUsage(current);
        setHistory(hist);
        setBreakdown(brkdwn);
        setError(null);
      } catch (err) {
        console.error("Failed to fetch usage stats:", err);
        setError(err instanceof Error ? err.message : t("failedToLoad"));
      } finally {
        setIsLoading(false);
      }
    };

    // Expose refresh function to parent via ref
    useImperativeHandle(ref, () => ({
      refresh: fetchAllStats,
    }));

    useEffect(() => {
      // Re-fetch when context changes (context for user scope, workspace for workspace scope)
      const shouldFetch =
        scope === "workspace"
          ? currentWorkspaceId !== null
          : contextId !== null;

      if (shouldFetch) {
        fetchAllStats();
      }
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [scope === "workspace" ? currentWorkspaceId : contextId, scope]);

    if (isLoading) {
      return (
        <div className="space-y-6">
          {[...Array(3)].map((_, i) => (
            <Card key={i}>
              <CardContent className="p-6">
                <div className="h-48 animate-pulse rounded bg-gray-200 dark:bg-gray-700" />
              </CardContent>
            </Card>
          ))}
        </div>
      );
    }

    if (error || !currentUsage || !history || !breakdown) {
      return (
        <Alert variant="destructive">
          <XCircle className="h-4 w-4" />
          <AlertDescription>{error || t("failedToLoad")}</AlertDescription>
        </Alert>
      );
    }

    return (
      <div className="space-y-6">
        {/* Quota Warnings - Issue #149 */}
        <QuotaWarning
          current={currentUsage.usage.memory_count}
          limit={currentUsage.plan.memory_limit}
          label={t("memories")}
          onUpgrade={() => {
            // TODO: Navigate to billing/upgrade page
          }}
        />

        {/* Current Usage Cards */}
        <div>
          <h2 className="text-2xl font-bold mb-4">
            {scope === "workspace" ? t("usage") : t("currentUsage")} -{" "}
            {currentUsage.plan.plan_name.toUpperCase()} {t("plan")}
          </h2>

          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {/* Memories */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium flex items-center gap-2">
                  <Brain className="h-4 w-4" />
                  {t("memories")}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold mb-2">
                  {currentUsage.usage.memory_count} /{" "}
                  {currentUsage.plan.memory_limit}
                </div>
                <Progress
                  value={Math.min(currentUsage.memory_usage.percentage, 100)}
                  className="h-2"
                />
                <p className="text-xs text-muted-foreground mt-2">
                  {t("percentUsed", {
                    percent: currentUsage.memory_usage.percentage.toFixed(1),
                  })}
                </p>
              </CardContent>
            </Card>

            {/* API Calls Today */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium flex items-center gap-2">
                  <Zap className="h-4 w-4" />
                  {t("apiCallsToday")}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold mb-2">
                  {currentUsage.usage.api_calls_today} /{" "}
                  {currentUsage.plan.daily_total_limit}
                </div>
                <Progress
                  value={Math.min(currentUsage.daily_api_usage.percentage, 100)}
                  className="h-2"
                />
                <p className="text-xs text-muted-foreground mt-2">
                  {t("percentUsed", {
                    percent: currentUsage.daily_api_usage.percentage.toFixed(1),
                  })}
                </p>
              </CardContent>
            </Card>

            {/* API Calls This Week */}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-medium flex items-center gap-2">
                  <TrendingUp className="h-4 w-4" />
                  {t("apiCallsThisWeek")}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold mb-2">
                  {currentUsage.usage.api_calls_this_week} /{" "}
                  {currentUsage.plan.weekly_total_limit}
                </div>
                <Progress
                  value={Math.min(
                    currentUsage.weekly_api_usage.percentage,
                    100,
                  )}
                  className="h-2"
                />
                <p className="text-xs text-muted-foreground mt-2">
                  {t("percentUsed", {
                    percent:
                      currentUsage.weekly_api_usage.percentage.toFixed(1),
                  })}
                </p>
              </CardContent>
            </Card>

            {/* Sleep-enabled Contexts (Issue #560) — only rendered when the
                backend included the field. Workspace-scoped only; user-scoped
                /usage/current returns null when no workspace is selected. */}
            {currentUsage.usage.sleep_contexts && (
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium flex items-center gap-2">
                    <Moon className="h-4 w-4" />
                    {t("sleepEnabledContexts")}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold mb-2">
                    {currentUsage.usage.sleep_contexts.used} /{" "}
                    {currentUsage.usage.sleep_contexts.limit}
                  </div>
                  <Progress
                    value={
                      currentUsage.usage.sleep_contexts.limit > 0
                        ? Math.min(
                            (currentUsage.usage.sleep_contexts.used /
                              currentUsage.usage.sleep_contexts.limit) *
                              100,
                            100,
                          )
                        : 0
                    }
                    className="h-2"
                  />
                  <p className="text-xs text-muted-foreground mt-2">
                    {/* Gate the "Includes +N from addon" hint on limit > 0
                        too — backend normalizes addon_bonus to 0 for zero-base
                        tiers (FREE/BASIC), but the explicit check makes the
                        intent obvious to readers and survives any future
                        backend regression. */}
                    {currentUsage.usage.sleep_contexts.limit > 0 &&
                    currentUsage.usage.sleep_contexts.addon_bonus > 0
                      ? t("sleepContextsWithAddon", {
                          addon: currentUsage.usage.sleep_contexts.addon_bonus,
                        })
                      : t("sleepContextsTier")}
                  </p>
                </CardContent>
              </Card>
            )}

            {/* Owned Workspaces (Issue #661) — user-level cap, unlike
                sleep_contexts which is workspace-scoped. Always present
                in the response (backend populates it independently of
                current_workspace_id), so no null gate. */}
            {currentUsage.usage.workspaces && (
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm font-medium flex items-center gap-2">
                    <Briefcase className="h-4 w-4" />
                    {t("ownedWorkspaces")}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="text-2xl font-bold mb-2">
                    {currentUsage.usage.workspaces.used} /{" "}
                    {currentUsage.usage.workspaces.limit}
                  </div>
                  <Progress
                    value={
                      currentUsage.usage.workspaces.limit > 0
                        ? Math.min(
                            (currentUsage.usage.workspaces.used /
                              currentUsage.usage.workspaces.limit) *
                              100,
                            100,
                          )
                        : 0
                    }
                    className="h-2"
                  />
                  <p className="text-xs text-muted-foreground mt-2">
                    {t("ownedWorkspacesTier")}
                  </p>
                </CardContent>
              </Card>
            )}
          </div>

          {/* API Breakdown - Issue #238: MCP/REST/Public separation,
              Issue #198: now also shows the per-tier daily limits so the
              numbers match what's in plan_tiers.py and the rate limiter. */}
          <div className="mt-4">
            <h3 className="text-sm font-medium text-gray-600 dark:text-gray-400 mb-3">
              API Call Breakdown (Today)
            </h3>
            <div className="grid gap-3 md:grid-cols-3">
              {/* MCP Calls */}
              <div className="p-3 border rounded-lg bg-blue-50 dark:bg-blue-950/20">
                <div className="text-xs text-gray-600 dark:text-gray-400 mb-1">
                  MCP Calls
                </div>
                <div className="text-lg font-bold text-blue-600">
                  {currentUsage.usage.mcp_calls_today}
                  {currentUsage.plan.mcp_calls_per_day > 0 && (
                    <span className="text-xs text-gray-500 dark:text-gray-400 font-normal ml-1">
                      / {currentUsage.plan.mcp_calls_per_day.toLocaleString()}
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400">
                  {currentUsage.usage.api_calls_today > 0
                    ? `${((currentUsage.usage.mcp_calls_today / currentUsage.usage.api_calls_today) * 100).toFixed(1)}%`
                    : "0%"}
                </div>
              </div>

              {/* REST Calls */}
              <div className="p-3 border rounded-lg bg-green-50 dark:bg-green-950/20">
                <div className="text-xs text-gray-600 dark:text-gray-400 mb-1">
                  REST API
                </div>
                <div className="text-lg font-bold text-green-600">
                  {currentUsage.usage.rest_calls_today}
                  {currentUsage.plan.rest_calls_per_day > 0 && (
                    <span className="text-xs text-gray-500 dark:text-gray-400 font-normal ml-1">
                      / {currentUsage.plan.rest_calls_per_day.toLocaleString()}
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400">
                  {currentUsage.usage.api_calls_today > 0
                    ? `${((currentUsage.usage.rest_calls_today / currentUsage.usage.api_calls_today) * 100).toFixed(1)}%`
                    : "0%"}
                </div>
              </div>

              {/* Public Calls */}
              <div className="p-3 border rounded-lg bg-purple-50 dark:bg-purple-950/20">
                <div className="text-xs text-gray-600 dark:text-gray-400 mb-1">
                  Public API
                </div>
                <div className="text-lg font-bold text-purple-600">
                  {currentUsage.usage.public_calls_today}
                  {currentUsage.plan.public_calls_per_day > 0 && (
                    <span className="text-xs text-gray-500 dark:text-gray-400 font-normal ml-1">
                      /{" "}
                      {currentUsage.plan.public_calls_per_day.toLocaleString()}
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400">
                  {currentUsage.usage.api_calls_today > 0
                    ? `${((currentUsage.usage.public_calls_today / currentUsage.usage.api_calls_today) * 100).toFixed(1)}%`
                    : "0%"}
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Usage Trend (7 days) */}
        <Card>
          <CardHeader>
            <CardTitle>{t("usageTrend")}</CardTitle>
            <CardDescription>{t("apiCallsPerDay")}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={history.daily_stats}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis
                    dataKey="date"
                    tickFormatter={(date) =>
                      formatDate(date, user?.timezone, locale)
                    }
                  />
                  <YAxis />
                  <Tooltip
                    labelFormatter={(date) =>
                      formatDate(date, user?.timezone, locale)
                    }
                  />
                  <Line
                    type="monotone"
                    dataKey="count"
                    stroke="#10b981"
                    strokeWidth={2}
                    dot={{ fill: "#10b981", r: 4 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        {/* Endpoint Breakdown — list-only. The pie chart was dropped:
            it duplicated the list and the per-slice labels overlapped
            once the long-tail endpoints were rolled into "Other". */}
        <Card>
          <CardHeader>
            <CardTitle>{t("usageByEndpoint")}</CardTitle>
            <CardDescription>{t("apiMcpDistribution")}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {topEndpoints.length > 0 ? (
                topEndpoints.map((ep, index) => (
                  <div
                    key={ep.endpoint}
                    className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-800 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                  >
                    <div className="flex items-center gap-2">
                      <div
                        className={`w-3 h-3 rounded-full ${SWATCH_BG_CLASSES[index % SWATCH_BG_CLASSES.length]}`}
                      />
                      <span className="text-sm font-medium">{ep.endpoint}</span>
                    </div>
                    <div className="text-right">
                      <div className="text-sm font-bold">{ep.count}</div>
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        {ep.percentage.toFixed(1)}%
                      </div>
                    </div>
                  </div>
                ))
              ) : (
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  {t("noUsageData")}
                </p>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    );
  },
);

UsageStats.displayName = "UsageStats";
