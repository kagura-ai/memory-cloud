"use client";

/**
 * Individual Context Memory Statistics Page
 *
 * Shows stats for a specific context (not necessarily the current one).
 * Allows viewing stats without switching the current context.
 * Issue #223: i18n support
 */

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useParams } from "next/navigation";
import Link from "next/link";
import {
  RichMemoryOverview,
  RichMemoryOverviewRef,
} from "@/components/dashboard/RichMemoryOverview";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
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
import {
  RefreshCw,
  Lock,
  Users,
  ChevronRight,
  Network,
  Check,
  ArrowRightCircle,
  TrendingUp,
  UserCircle,
} from "lucide-react";
import { InlineSpinner } from "@/components/common/LoadingState";
import { useMemoryContext } from "@/contexts/MemoryContextContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getContext } from "@/lib/api/contexts";
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

export default function ContextStatsPage() {
  const t = useTranslations("contextStats");
  const { user } = useAuth();

  const params = useParams();
  const contextId = params.id as string;

  const overviewRef = useRef<RichMemoryOverviewRef>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [context, setContext] = useState<Context | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [timeline, setTimeline] = useState<ContextUsageTimelineResponse | null>(
    null,
  );
  const [userActivity, setUserActivity] =
    useState<ContextUserActivityResponse | null>(null);
  const [publicAPIStats, setPublicAPIStats] =
    useState<PublicAPIStatsResponse | null>(null);
  const [timelineDays, setTimelineDays] = useState<7 | 30>(7);
  const { currentContext } = useMemoryContext();
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();

  // Fetch context info
  useEffect(() => {
    const fetchContext = async () => {
      try {
        setLoading(true);
        setError(null);
        const ctx = await getContext(contextId);
        setContext(ctx);
      } catch (err) {
        console.error("Failed to fetch context:", err);
        setError(t("failedToLoadContext"));
      } finally {
        setLoading(false);
      }
    };

    if (contextId) {
      fetchContext();
    }
  }, [contextId]);

  // Fetch usage statistics
  useEffect(() => {
    const fetchUsageStats = async () => {
      if (!contextId || !currentWorkspaceId) return;

      try {
        const timelineData = await getContextUsageTimeline(
          currentWorkspaceId,
          contextId,
          timelineDays,
        );
        setTimeline(timelineData);

        // Try to fetch user activity (Admin/Owner only)
        try {
          const activityData = await getContextUserActivity(
            currentWorkspaceId,
            contextId,
            timelineDays,
          );
          setUserActivity(activityData);
          // P0-2: Removed development console.log
        } catch (activityErr: any) {
          if (process.env.NODE_ENV === "development") {
            console.error("User activity fetch failed:", activityErr);
          }
          setUserActivity(null);
        }

        // Issue #265: Fetch public API stats if context is public
        if (context?.is_public) {
          try {
            const publicStats = await getContextPublicAPIStats(
              currentWorkspaceId,
              contextId,
              timelineDays,
            );
            setPublicAPIStats(publicStats);
          } catch (err) {
            if (process.env.NODE_ENV === "development") {
              console.error("Failed to fetch public API stats:", err);
            }
            // P1-4: Keep null for graceful degradation (no toast to avoid noise)
            setPublicAPIStats(null);
          }
        }
      } catch (err) {
        if (process.env.NODE_ENV === "development") {
          console.error("Failed to fetch usage stats:", err);
        }
      }
    };

    if (context && currentWorkspaceId) {
      fetchUsageStats();
    }
  }, [context, contextId, currentWorkspaceId, timelineDays]);

  useEffect(() => {
    const title = context?.display_name || context?.name || t("title");
    document.title = `${title} - Kagura Memory Cloud`;
  }, [context, t]);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    try {
      await overviewRef.current?.refresh();
      // Refresh usage stats too
      if (currentWorkspaceId && contextId) {
        const timelineData = await getContextUsageTimeline(
          currentWorkspaceId,
          contextId,
          timelineDays,
        );
        setTimeline(timelineData);

        try {
          const activityData = await getContextUserActivity(
            currentWorkspaceId,
            contextId,
            timelineDays,
          );
          setUserActivity(activityData);
        } catch (activityErr) {
          if (process.env.NODE_ENV === "development") {
            console.error("User activity refresh failed:", activityErr);
          }
          setUserActivity(null);
        }

        // P1-5: Re-fetch public API stats if context is public
        if (context?.is_public) {
          try {
            const publicStats = await getContextPublicAPIStats(
              currentWorkspaceId,
              contextId,
              timelineDays,
            );
            setPublicAPIStats(publicStats);
          } catch (err) {
            if (process.env.NODE_ENV === "development") {
              console.error("Failed to refresh public API stats:", err);
            }
            setPublicAPIStats(null);
          }
        }
      }
    } finally {
      setIsRefreshing(false);
    }
  };

  if (loading) {
    return (
      <PageContainer>
        <div className="flex items-center justify-center py-12">
          <InlineSpinner size="lg" />
          <span className="ml-2 text-sm text-gray-500">
            {t("loadingContext")}
          </span>
        </div>
      </PageContainer>
    );
  }

  if (error || !context) {
    return (
      <PageContainer>
        <div className="text-center py-12">
          <p className="text-red-600">{error || t("contextNotFound")}</p>
          <Link href="/workspace/contexts">
            <Button variant="outline" className="mt-4">
              {t("backToContexts")}
            </Button>
          </Link>
        </div>
      </PageContainer>
    );
  }

  const isCurrent = currentContext?.id === context.id;
  const displayName = context.display_name || context.name;

  // Build enhanced title with privacy indicator
  const privacyIcon = context.is_private ? (
    <Lock
      className="h-5 w-5 text-gray-400 inline-block mr-2"
      aria-label={t("privateContext")}
    />
  ) : (
    <Users
      className="h-5 w-5 text-blue-500 inline-block mr-2"
      aria-label={t("sharedContext")}
    />
  );

  const pageTitle = (
    <div className="flex items-center gap-2">
      {privacyIcon}
      <span>{t("titleWithContext", { contextName: displayName })}</span>
    </div>
  );

  // Build detailed description with workspace and privacy info
  const privacyLabel = context.is_private
    ? `🔒 ${t("privateContext")} - ${t("privateContextDesc")}`
    : `👥 ${t("sharedContext")} - ${t("sharedContextDesc")}`;

  const workspaceInfo = currentWorkspace?.name
    ? `${t("workspace")}: ${currentWorkspace.name}${currentWorkspace.description ? ` - ${currentWorkspace.description}` : ""}`
    : "";

  const pageDescription = workspaceInfo
    ? `${privacyLabel} | ${workspaceInfo}`
    : privacyLabel;

  return (
    <PageContainer>
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-4">
        <Link
          href="/workspace/contexts"
          className="hover:text-gray-900 dark:hover:text-gray-200 hover:underline"
        >
          {t("breadcrumbContexts")}
        </Link>
        <ChevronRight className="h-4 w-4" />
        <div className="flex items-center gap-2">
          <span className="text-gray-900 dark:text-gray-100">
            {displayName}
          </span>
          {isCurrent && (
            <span className="px-2 py-0.5 text-xs bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full font-medium flex items-center gap-1">
              <Check className="h-3 w-3" />
              {t("current")}
            </span>
          )}
        </div>
      </nav>

      <PageHeader
        title={pageTitle}
        description={pageDescription}
        actions={
          <div className="flex items-center gap-2">
            <Link href={`/workspace/contexts/${contextId}/graph`}>
              <Button variant="outline" size="sm">
                <Network className="h-4 w-4 mr-2" />
                {t("viewGraph")}
              </Button>
            </Link>
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
        }
      />

      {/* Usage Timeline Chart */}
      {timeline && timeline.daily_usage.length > 0 && (
        <Card className="mb-6">
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
                  tickFormatter={(date) => formatDate(date, user?.timezone)}
                />
                <YAxis />
                <Tooltip
                  labelFormatter={(date) => formatDate(date, user?.timezone)}
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

      {/* Issue #265: Public API Usage Stats (Public contexts only) */}
      {context?.is_public && publicAPIStats && (
        <div className="mt-6">
          <h2 className="text-xl font-semibold mb-4">{t("publicAPIUsage")}</h2>
          <PublicAPIStats stats={publicAPIStats} days={timelineDays} />
        </div>
      )}

      {/* Memory Health & Neural Activity */}
      <RichMemoryOverview ref={overviewRef} contextId={contextId} />

      {/* User Activity Table */}
      <Card className="mt-6">
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
                  <TableHead className="text-right">{t("apiCalls")}</TableHead>
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
                        ? formatRelativeTime(user.last_activity, user?.timezone)
                        : "Never"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <div className="text-center py-8 text-gray-500">
              {userActivity === null ? (
                <div>
                  <p>{t("userActivityNotAvailable")}</p>
                  <p className="text-sm mt-2">{t("requiresAdminRole")}</p>
                </div>
              ) : (
                <p>{t("noActivityData")}</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </PageContainer>
  );
}
