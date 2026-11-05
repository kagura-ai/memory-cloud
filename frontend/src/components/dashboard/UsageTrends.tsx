"use client";

import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CardLoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LineChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis, CartesianGrid } from "recharts";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useAuth } from "@/contexts/AuthContext";
import { getWorkspaceUsageHistory, getWorkspaceUsageBreakdown } from "@/lib/api/workspaces";
import type { UsageHistoryResponse, UsageBreakdownResponse, EndpointUsage } from "@/lib/api/usage";
import { formatDate } from "@/lib/utils/datetime";

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


export interface UsageTrendsRef {
  refresh: () => Promise<void>;
}

export const UsageTrends = forwardRef<UsageTrendsRef>(function UsageTrends(_, ref) {
  const t = useTranslations("usageStats");
  const locale = useLocale();
  const { currentWorkspaceId } = useWorkspace();
  const { user } = useAuth();
  const [history, setHistory] = useState<UsageHistoryResponse | null>(null);
  const [breakdown, setBreakdown] = useState<UsageBreakdownResponse | null>(null);
  const [loadedWorkspaceId, setLoadedWorkspaceId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const cancelPending = useCallback(() => { requestId.current++; }, []);

  const refresh = useCallback(async () => {
    if (!currentWorkspaceId) return;
    const request = ++requestId.current;
    setIsLoading(true);
    setError(null);
    try {
      const [hist, endpoints] = await Promise.all([
        getWorkspaceUsageHistory(7),
        getWorkspaceUsageBreakdown(30),
      ]);
      if (request !== requestId.current) return;
      setHistory(hist);
      setBreakdown(endpoints);
      setLoadedWorkspaceId(currentWorkspaceId);
    } catch (err) {
      if (request !== requestId.current) return;
      setError(err instanceof Error ? err.message : t("failedToLoad"));
      setLoadedWorkspaceId(currentWorkspaceId);
    } finally {
      if (request === requestId.current) setIsLoading(false);
    }
  }, [currentWorkspaceId, t]);

  useImperativeHandle(ref, () => ({ refresh }), [refresh]);
  useEffect(() => {
    void refresh();
    return cancelPending;
  }, [refresh, cancelPending]);

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


  if (isLoading || loadedWorkspaceId !== currentWorkspaceId) return <CardLoadingState count={2} />;
  if (error || !history || !breakdown) return <ErrorBanner error={error || t("failedToLoad")} />;

  return <div className="space-y-6">
        {/* Usage Trend (7 days) */}
        <Card>
          <CardHeader>
            <CardTitle>{t("usageTrend")}</CardTitle>
            <CardDescription>{t("apiCallsPerDay")}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-64">
              <ResponsiveContainer width="100%" height={256}>
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

  </div>;
});
