"use client";

/**
 * Usage Statistics Component
 *
 * Shows plan limits, current usage, and usage trends.
 * Issue #48 - Usage Statistics - Plan Limits & Usage Tracking
 * Issue #223 - i18n support
 */

import { useEffect, useState, forwardRef, useImperativeHandle } from "react";
import { useTranslations } from "next-intl";
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
  PieChart as RechartsPie,
  Pie,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  CartesianGrid,
} from "recharts";
import {
  Brain,
  Database,
  Zap,
  TrendingUp,
  AlertTriangle,
  XCircle,
} from "lucide-react";
import { apiClient } from "@/lib/api";
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

interface PlanLimits {
  plan_name: string;
  memory_limit: number;
  daily_api_limit: number;
  weekly_api_limit: number;
}

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

const COLORS = [
  "#10b981",
  "#3b82f6",
  "#8b5cf6",
  "#f59e0b",
  "#ef4444",
  "#06b6d4",
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
    const [currentUsage, setCurrentUsage] = useState<UsageCurrentData | null>(
      null,
    );
    const [history, setHistory] = useState<UsageHistory | null>(null);
    const [breakdown, setBreakdown] = useState<UsageBreakdown | null>(null);
    const [isLoading, setIsLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

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

    const getProgressColor = (status: UsageStatus) => {
      if (status.is_exceeded) return "bg-red-600";
      if (status.is_critical) return "bg-orange-600";
      if (status.is_warning) return "bg-yellow-500";
      return "bg-brand-green-600";
    };

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
                  {currentUsage.plan.daily_api_limit}
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
                  {currentUsage.plan.weekly_api_limit}
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
          </div>

          {/* API Breakdown - Issue #238: MCP/REST/Public separation */}
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
                    tickFormatter={(date) => formatDate(date, user?.timezone)}
                  />
                  <YAxis />
                  <Tooltip
                    labelFormatter={(date) => formatDate(date, user?.timezone)}
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

        {/* Endpoint Breakdown */}
        <div className="grid gap-4 md:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>{t("usageByEndpoint")}</CardTitle>
              <CardDescription>{t("apiMcpDistribution")}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="h-64">
                {breakdown.by_endpoint.length > 0 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <RechartsPie>
                      <Pie
                        data={breakdown.by_endpoint as any}
                        cx="50%"
                        cy="50%"
                        labelLine={false}
                        label={(entry: any) =>
                          `${entry.endpoint}: ${entry.percentage.toFixed(1)}%`
                        }
                        outerRadius={80}
                        dataKey="count"
                      >
                        {breakdown.by_endpoint.map((entry, index) => (
                          <Cell
                            key={`cell-${index}`}
                            fill={COLORS[index % COLORS.length]}
                          />
                        ))}
                      </Pie>
                      <Tooltip />
                    </RechartsPie>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <p className="text-sm text-gray-500 dark:text-gray-400">
                      {t("noUsageData")}
                    </p>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>

          {/* Endpoint List */}
          <Card>
            <CardHeader>
              <CardTitle>{t("endpointDetails")}</CardTitle>
              <CardDescription>{t("requestCountByEndpoint")}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                {breakdown.by_endpoint.length > 0 ? (
                  breakdown.by_endpoint.map((ep, index) => (
                    <div
                      key={ep.endpoint}
                      className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-800 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                    >
                      <div className="flex items-center gap-2">
                        <div
                          className="w-3 h-3 rounded-full"
                          style={{
                            backgroundColor: COLORS[index % COLORS.length],
                          }}
                        />
                        <span className="text-sm font-medium">
                          {ep.endpoint}
                        </span>
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
      </div>
    );
  },
);

UsageStats.displayName = "UsageStats";
