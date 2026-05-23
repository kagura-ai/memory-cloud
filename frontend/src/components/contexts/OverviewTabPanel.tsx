/**
 * OverviewTabPanel
 *
 * Self-contained panel for the Overview tab in the consolidated context detail page.
 * Contains usage timeline, public API stats, memory health overview, and user activity.
 * Extracted from contexts/[id]/stats/page.tsx (#232).
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations, useLocale } from "next-intl";
import {
  RichMemoryOverview,
  RichMemoryOverviewRef,
} from "@/components/dashboard/RichMemoryOverview";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  CartesianGrid,
} from "recharts";
import { RefreshCw, TrendingUp, UserCircle } from "lucide-react";
import { InlineSpinner } from "@/components/common/LoadingState";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import {
  getContextUsageTimeline,
  getContextUserActivity,
  getContextPublicAPIStats,
  type ContextUsageTimelineResponse,
  type ContextUserActivityResponse,
  type PublicAPIStatsResponse,
} from "@/lib/api/workspaces";
import { PublicAPIStats } from "@/components/dashboard/PublicAPIStats";
import { formatRelativeTime, formatDate } from "@/lib/utils/datetime";
import type { Context } from "@/lib/types/context";
import { useAuth } from "@/contexts/AuthContext";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";

interface OverviewTabPanelProps {
  contextId: string;
  context: Context;
}

export function OverviewTabPanel({
  contextId,
  context,
}: OverviewTabPanelProps) {
  const t = useTranslations("contextStats");
  const { user: authUser } = useAuth();
  const locale = useLocale();

  const overviewRef = useRef<RichMemoryOverviewRef>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [timeline, setTimeline] = useState<ContextUsageTimelineResponse | null>(
    null,
  );
  const [userActivity, setUserActivity] =
    useState<ContextUserActivityResponse | null>(null);
  const [publicAPIStats, setPublicAPIStats] =
    useState<PublicAPIStatsResponse | null>(null);
  const [timelineDays, setTimelineDays] = useState<7 | 30>(7);
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  // Issue #398: User Activity is admin/owner only. Backend 403's the
  // /user-activity endpoint for non-admins; gating here both hides the
  // card and skips the doomed API call.
  const canSeeUserActivity = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Admin,
  );

  const fetchUsageStats = useCallback(async () => {
    if (!contextId || !currentWorkspaceId) return;

    try {
      const timelineData = await getContextUsageTimeline(
        currentWorkspaceId,
        contextId,
        timelineDays,
      );
      setTimeline(timelineData);

      if (canSeeUserActivity) {
        try {
          const activityData = await getContextUserActivity(
            currentWorkspaceId,
            contextId,
            timelineDays,
          );
          setUserActivity(activityData);
        } catch {
          setUserActivity(null);
        }
      }

      if (context?.is_public) {
        try {
          const publicStats = await getContextPublicAPIStats(
            currentWorkspaceId,
            contextId,
            timelineDays,
          );
          setPublicAPIStats(publicStats);
        } catch {
          setPublicAPIStats(null);
        }
      }
    } catch {
      // Timeline fetch failed — graceful degradation
    }
  }, [
    contextId,
    currentWorkspaceId,
    timelineDays,
    context,
    canSeeUserActivity,
  ]);

  useEffect(() => {
    if (context && currentWorkspaceId) {
      fetchUsageStats();
    }
  }, [context, currentWorkspaceId, fetchUsageStats]);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    try {
      await overviewRef.current?.refresh();
      await fetchUsageStats();
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Refresh button */}
      <div className="flex justify-end">
        <Button
          onClick={handleRefresh}
          variant="outline"
          size="sm"
          disabled={isRefreshing}
        >
          {isRefreshing ? (
            <InlineSpinner size="sm" className="mr-2" />
          ) : (
            <RefreshCw className="h-4 w-4 mr-2" />
          )}
          {t("refresh")}
        </Button>
      </div>

      {/* Usage Timeline Chart */}
      {timeline && timeline.daily_usage.length > 0 && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <TrendingUp className="h-5 w-5" />
                  {t("usageTimeline")}
                </CardTitle>
                <CardDescription>
                  {timelineDays === 7
                    ? t("apiCallsLast7Days")
                    : t("apiCallsLast30Days")}
                </CardDescription>
              </div>
              <div className="flex gap-2">
                <Button
                  variant={timelineDays === 7 ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTimelineDays(7)}
                >
                  7{t("days")}
                </Button>
                <Button
                  variant={timelineDays === 30 ? "default" : "outline"}
                  size="sm"
                  onClick={() => setTimelineDays(30)}
                >
                  30{t("days")}
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={timeline.daily_usage}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tickFormatter={(date) =>
                    formatDate(date, authUser?.timezone, locale)
                  }
                />
                <YAxis />
                <Tooltip
                  labelFormatter={(date) =>
                    formatDate(date, authUser?.timezone, locale)
                  }
                  formatter={(value: number, name: string) => [
                    value,
                    name === "API Calls" ? t("apiCalls") : t("uniqueUsers"),
                  ]}
                />
                <Line
                  type="monotone"
                  dataKey="api_calls"
                  stroke="#10b981"
                  strokeWidth={2}
                  name="API Calls"
                />
                <Line
                  type="monotone"
                  dataKey="unique_users"
                  stroke="#3b82f6"
                  strokeWidth={2}
                  name="Unique Users"
                />
              </LineChart>
            </ResponsiveContainer>
            <div className="mt-4 text-sm text-gray-500">
              {timelineDays === 7
                ? t("totalCallsLast7Days")
                : t("totalCallsLast30Days")}
              :{" "}
              <span className="font-semibold text-green-600">
                {timeline.total_calls.toLocaleString()}
              </span>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Public API Usage Stats */}
      {context?.is_public && publicAPIStats && (
        <div>
          <h2 className="text-xl font-semibold mb-4">{t("publicAPIUsage")}</h2>
          <PublicAPIStats stats={publicAPIStats} days={timelineDays} />
        </div>
      )}

      {/* Memory Health & Neural Activity */}
      <RichMemoryOverview ref={overviewRef} contextId={contextId} />

      {/* User Activity Table — Issue #398: admin/owner only. */}
      {canSeeUserActivity && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <UserCircle className="h-5 w-5" />
              {t("userActivity")}
            </CardTitle>
            <CardDescription>
              {timelineDays === 7
                ? t("topUsersLast7Days")
                : t("topUsersLast30Days")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {userActivity && userActivity.users.length > 0 ? (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("user")}</TableHead>
                    <TableHead className="text-right">
                      {t("apiCalls")}
                    </TableHead>
                    <TableHead className="text-right">
                      {t("lastActivity")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {userActivity.users.map((user) => (
                    <TableRow key={user.user_id}>
                      <TableCell>
                        <div>
                          <div className="font-medium">
                            {user.user_name || user.user_email}
                          </div>
                          {user.user_name && user.user_email && (
                            <div className="text-sm text-gray-500">
                              {user.user_email}
                            </div>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-right">
                        <span className="font-semibold text-green-600">
                          {user.api_calls.toLocaleString()}
                        </span>
                      </TableCell>
                      <TableCell className="text-right text-sm text-gray-500">
                        {user.last_activity
                          ? formatRelativeTime(user.last_activity, locale)
                          : t("never")}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <div className="text-center py-8 text-gray-500">
                {/* The card is gated to admin/owner via canSeeUserActivity
                    above, so reaching the null branch means a real fetch
                    failure (network/5xx) rather than an authorization
                    denial. The pre-gating "requires admin role" sub-copy
                    would be misleading here. */}
                {userActivity === null ? (
                  <p>{t("userActivityNotAvailable")}</p>
                ) : (
                  <p>{t("noActivityData")}</p>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
