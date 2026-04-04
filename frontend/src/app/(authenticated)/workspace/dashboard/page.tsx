"use client";

/**
 * Workspace Statistics Page
 *
 * Issue #115 - Workspace-level Multi-tenancy Support
 * Shows aggregated statistics across all user's contexts.
 */

import { useEffect, useState, useRef } from "react";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  RefreshCw,
  Database,
  HardDrive,
  FolderOpen,
  AlertCircle,
  Lock,
  Users,
  Download,
  ArrowUpDown,
  Calendar,
} from "lucide-react";
import { apiClient } from "@/lib/api/base";
import { InlineSpinner } from "@/components/common/LoadingState";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { UsageStats } from "@/components/dashboard/UsageStats";
import { PlanBadge } from "@/components/common/PlanBadge";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import {
  getContextStats,
  ContextStatsResponse,
  getWorkspaceMemoryTimeline,
  MemoryTimelineResponse,
  getContextUserActivity,
  ContextUserActivityResponse,
  getWorkspaceMemberUsage,
  MemberUsageEntry,
} from "@/lib/api/workspaces";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import { formatDistanceToNow } from "date-fns";
import Link from "next/link";

interface PrivateContextAggregation {
  context_count: number;
  memory_count: number;
}

interface ContextStats {
  context_id: string;
  context_name: string;
  created_by: string | null;
  created_by_name: string | null;
  memory_count: number;
  is_private?: boolean; // Issue #165: Privacy flag
}

interface WorkspaceStats {
  total_memories: number;
  context_count: number;
  contexts: ContextStats[];
  private_aggregation?: PrivateContextAggregation | null; // Issue #165
  plan_name: string; // Issue #149
}

function MemberUsageSection() {
  const t = useTranslations("dashboard");
  const [members, setMembers] = useState<MemberUsageEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getWorkspaceMemberUsage()
      .then((data) => setMembers(data.members))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) return null;
  if (members.length <= 1) return null; // Solo workspace, no need to show

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Users className="h-5 w-5" />
          {t("memberUsage", { default: "Member Usage" })}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("member", { default: "Member" })}</TableHead>
              <TableHead className="text-right">
                {t("memories", { default: "Memories" })}
              </TableHead>
              <TableHead className="text-right">
                {t("apiToday", { default: "API Today" })}
              </TableHead>
              <TableHead className="text-right">
                {t("apiWeek", { default: "API Week" })}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((m) => (
              <TableRow key={m.user_id}>
                <TableCell>
                  <div>
                    <div className="font-medium">{m.name || "Unknown"}</div>
                    {m.email && (
                      <div className="text-xs text-muted-foreground">
                        {m.email}
                      </div>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  {m.memory_count.toLocaleString()}
                </TableCell>
                <TableCell className="text-right">
                  {m.api_calls_today.toLocaleString()}
                </TableCell>
                <TableCell className="text-right">
                  {m.api_calls_week.toLocaleString()}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export default function WorkspaceStatsPage() {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  const [stats, setStats] = useState<WorkspaceStats | null>(null);
  const [contextStats, setContextStats] = useState<ContextStatsResponse | null>(
    null,
  );
  const [memoryTimeline, setMemoryTimeline] =
    useState<MemoryTimelineResponse | null>(null);
  const [timelineDays, setTimelineDays] = useState<7 | 30>(30);
  const [userActivity, setUserActivity] =
    useState<ContextUserActivityResponse | null>(null);
  const [selectedContextId, setSelectedContextId] = useState<string | null>(
    null,
  );
  const [activityDays, setActivityDays] = useState<7 | 30>(7);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<"name" | "memory" | "activity">(
    "memory",
  );
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");

  const fetchStats = async () => {
    try {
      setLoading(true);
      setError(null);
      const [statsResponse, contextStatsResponse, timelineResponse] =
        await Promise.all([
          apiClient.get<WorkspaceStats>("/api/v1/workspace/stats"),
          currentWorkspaceId
            ? getContextStats(currentWorkspaceId)
            : Promise.resolve(null),
          currentWorkspaceId
            ? getWorkspaceMemoryTimeline(currentWorkspaceId, timelineDays)
            : Promise.resolve(null),
        ]);
      setStats(statsResponse);
      setContextStats(contextStatsResponse);
      setMemoryTimeline(timelineResponse);
    } catch (err) {
      console.error("Failed to fetch workspace stats:", err);
      setError(err instanceof Error ? err.message : t("failedToLoadStats"));
    } finally {
      setLoading(false);
    }
  };

  const handleSort = (column: "name" | "memory" | "activity") => {
    if (sortBy === column) {
      setSortOrder(sortOrder === "asc" ? "desc" : "asc");
    } else {
      setSortBy(column);
      setSortOrder("desc");
    }
  };

  const exportToCSV = () => {
    if (!contextStats) return;

    const headers = [
      "Context Name",
      "Memory Count",
      "Last Activity",
      "Members",
    ];
    const rows = contextStats.contexts.map((ctx) => [
      ctx.context_name,
      ctx.memory_count.toString(),
      ctx.last_activity || "Never",
      ctx.member_count.toString(),
    ]);

    const csv = [
      headers.join(","),
      ...rows.map((row) => row.map((cell) => `"${cell}"`).join(",")),
      "",
      `Total,${contextStats.workspace_totals.memory_count},,`,
    ].join("\n");

    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `context-stats-${currentWorkspace?.name.toLowerCase().replace(/\s+/g, "-") || "export"}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  useEffect(() => {
    fetchStats();
  }, []);

  // Load user activity when context is selected (Admin only)
  // Fix: AbortController to prevent race conditions
  useEffect(() => {
    const isAdmin =
      currentWorkspace?.current_user_role === "admin" ||
      currentWorkspace?.current_user_role === "owner";
    if (!isAdmin || !selectedContextId || !currentWorkspaceId) return;

    const controller = new AbortController();
    setActivityError(null);

    getContextUserActivity(currentWorkspaceId, selectedContextId, activityDays)
      .then((activity) => {
        if (!controller.signal.aborted) {
          setUserActivity(activity);
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          console.error("Failed to fetch user activity:", err);
          setActivityError(err?.message || "Failed to load user activity");
          setUserActivity(null);
        }
      });

    return () => controller.abort();
  }, [
    selectedContextId,
    activityDays,
    currentWorkspaceId,
    currentWorkspace?.current_user_role,
  ]);

  // Issue #275 Task 6 Fix: Reload timeline when day range or context filter changes
  useEffect(() => {
    if (currentWorkspaceId) {
      getWorkspaceMemoryTimeline(
        currentWorkspaceId,
        timelineDays,
        selectedContextId || undefined,
      )
        .then(setMemoryTimeline)
        .catch((err) => {
          console.error("Failed to load memory timeline:", err);
          setMemoryTimeline(null);
        });
    }
  }, [timelineDays, currentWorkspaceId, selectedContextId]);

  // Sort contexts based on selected column and order
  const sortedContexts = stats?.contexts
    ? [...stats.contexts].sort((a, b) => {
        const aDetail = contextStats?.contexts.find(
          (c) => c.context_id === a.context_id,
        );
        const bDetail = contextStats?.contexts.find(
          (c) => c.context_id === b.context_id,
        );

        let aValue: any;
        let bValue: any;

        switch (sortBy) {
          case "name":
            aValue = a.context_name.toLowerCase();
            bValue = b.context_name.toLowerCase();
            break;
          case "memory":
            aValue = a.memory_count;
            bValue = b.memory_count;
            break;
          case "activity":
            aValue = aDetail?.last_activity
              ? new Date(aDetail.last_activity).getTime()
              : 0;
            bValue = bDetail?.last_activity
              ? new Date(bDetail.last_activity).getTime()
              : 0;
            break;
        }

        if (aValue < bValue) return sortOrder === "asc" ? -1 : 1;
        if (aValue > bValue) return sortOrder === "asc" ? 1 : -1;
        return 0;
      })
    : [];

  return (
    <PageContainer>
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div>
            <h1 className="text-3xl font-bold">{t("overview")}</h1>
            <p className="text-muted-foreground">
              {currentWorkspace?.description || t("overviewDesc")}
            </p>
          </div>
          {stats && (
            <PlanBadge
              planName={stats.plan_name as "free" | "basic" | "pro"}
              size="lg"
            />
          )}
        </div>
        <div className="flex items-center gap-2">
          {stats?.contexts && stats.contexts.length > 0 && (
            <select
              value={selectedContextId || "all"}
              onChange={(e) =>
                setSelectedContextId(
                  e.target.value === "all" ? null : e.target.value,
                )
              }
              className="px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-800 text-sm"
              aria-label={t("filterByContext")}
            >
              <option value="all">{t("allContexts")}</option>
              {stats.contexts.map((ctx) => (
                <option key={ctx.context_id} value={ctx.context_id}>
                  {ctx.context_name}
                </option>
              ))}
            </select>
          )}
          <Button onClick={fetchStats} variant="outline" disabled={loading}>
            {loading ? (
              <InlineSpinner size="sm" className="mr-2" />
            ) : (
              <RefreshCw className="h-4 w-4 mr-2" />
            )}
            {t("refresh")}
          </Button>
        </div>
      </div>

      {error && (
        <Alert variant="destructive" className="mb-6">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>{tCommon("error")}</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {loading && !stats ? (
        <div className="flex items-center justify-center py-12">
          <InlineSpinner size="lg" />
          <span className="ml-3 text-slate-500">{t("loadingStats")}</span>
        </div>
      ) : stats ? (
        <>
          {/* Context Breakdown Section - only shown when viewing all contexts */}
          {!selectedContextId && (
            <div className="mb-6">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                    {t("contextBreakdown")}
                  </h2>
                  <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                    {t("memoryUsageByContext")}
                  </p>
                </div>
                <Button onClick={exportToCSV} variant="outline" size="sm">
                  <Download className="h-4 w-4 mr-2" />
                  Export CSV
                </Button>
              </div>

              <Card>
                <CardContent className="pt-6">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead
                          className="cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800"
                          onClick={() => handleSort("name")}
                        >
                          <div className="flex items-center gap-1">
                            {t("contextName")}
                            {sortBy === "name" && (
                              <ArrowUpDown className="h-3 w-3" />
                            )}
                          </div>
                        </TableHead>
                        <TableHead>{t("owner")}</TableHead>
                        <TableHead
                          className="text-right cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800"
                          onClick={() => handleSort("memory")}
                        >
                          <div className="flex items-center justify-end gap-1">
                            {t("memoriesCount")}
                            {sortBy === "memory" && (
                              <ArrowUpDown className="h-3 w-3" />
                            )}
                          </div>
                        </TableHead>
                        <TableHead
                          className="text-right cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800"
                          onClick={() => handleSort("activity")}
                        >
                          <div className="flex items-center justify-end gap-1">
                            {t("lastActivity")}
                            {sortBy === "activity" && (
                              <ArrowUpDown className="h-3 w-3" />
                            )}
                          </div>
                        </TableHead>
                        <TableHead className="text-right">
                          {t("apiCallsWeek")}
                        </TableHead>
                        <TableHead className="text-right">
                          {t("activeUsersWeek")}
                        </TableHead>
                        <TableHead className="text-right">
                          {t("members")}
                        </TableHead>
                        <TableHead className="text-right">
                          {t("percentOfTotal")}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {stats.contexts.length === 0 &&
                      !stats.private_aggregation ? (
                        <TableRow>
                          <TableCell
                            colSpan={9}
                            className="text-center text-muted-foreground py-8"
                          >
                            {t("noContextsFound")}
                          </TableCell>
                        </TableRow>
                      ) : (
                        <>
                          {/* Accessible contexts - full details */}
                          {sortedContexts.map((context) => {
                            const percentage =
                              stats.total_memories > 0
                                ? (
                                    (context.memory_count /
                                      stats.total_memories) *
                                    100
                                  ).toFixed(1)
                                : "0.0";
                            const contextDetail = contextStats?.contexts.find(
                              (c) => c.context_id === context.context_id,
                            );
                            return (
                              <TableRow key={context.context_id}>
                                <TableCell className="font-medium">
                                  <div className="flex items-center gap-2">
                                    {context.is_private ? (
                                      <Lock
                                        className="h-3 w-3 text-gray-400"
                                        aria-label={t("privateContext")}
                                        role="img"
                                      />
                                    ) : (
                                      <Users
                                        className="h-3 w-3 text-blue-500"
                                        aria-label={t("sharedContext")}
                                        role="img"
                                      />
                                    )}
                                    <Link
                                      href={`/workspace/contexts/${context.context_id}/stats`}
                                      className="text-indigo-600 hover:text-indigo-900 dark:text-indigo-400 dark:hover:text-indigo-300"
                                    >
                                      {context.context_name}
                                    </Link>
                                  </div>
                                </TableCell>
                                <TableCell className="text-sm text-gray-600 dark:text-gray-400">
                                  {context.created_by_name ||
                                    context.created_by ||
                                    t("notAvailable")}
                                </TableCell>
                                <TableCell className="text-right">
                                  {context.memory_count.toLocaleString()}
                                </TableCell>
                                <TableCell className="text-right text-sm text-gray-500">
                                  {contextDetail?.last_activity
                                    ? formatDistanceToNow(
                                        new Date(contextDetail.last_activity),
                                        { addSuffix: true },
                                      )
                                    : "Never"}
                                </TableCell>
                                <TableCell className="text-right">
                                  <span
                                    className={
                                      contextDetail?.api_calls_week &&
                                      contextDetail.api_calls_week > 0
                                        ? "text-green-600 font-medium"
                                        : "text-gray-400"
                                    }
                                  >
                                    {contextDetail?.api_calls_week?.toLocaleString() ||
                                      "0"}
                                  </span>
                                </TableCell>
                                <TableCell className="text-right">
                                  <span
                                    className={
                                      contextDetail?.active_users_week &&
                                      contextDetail.active_users_week > 0
                                        ? "text-blue-600 font-medium"
                                        : "text-gray-400"
                                    }
                                  >
                                    {contextDetail?.active_users_week || "0"}
                                  </span>
                                </TableCell>
                                <TableCell className="text-right">
                                  {contextDetail?.member_count || "-"}
                                </TableCell>
                                <TableCell className="text-right">
                                  {percentage}%
                                </TableCell>
                              </TableRow>
                            );
                          })}

                          {/* Inaccessible private contexts - aggregated row */}
                          {stats.private_aggregation &&
                            stats.private_aggregation.context_count > 0 && (
                              <TableRow className="bg-gray-50 dark:bg-gray-800/50">
                                <TableCell className="font-medium text-gray-600 dark:text-gray-400">
                                  <div className="flex items-center gap-2">
                                    <Lock
                                      className="h-3 w-3"
                                      aria-label={t("privateContext")}
                                      role="img"
                                    />
                                    <span className="italic">
                                      {t("othersPrivate", {
                                        count:
                                          stats.private_aggregation
                                            .context_count,
                                      })}
                                    </span>
                                  </div>
                                </TableCell>
                                <TableCell className="text-sm text-gray-500 dark:text-gray-500 italic">
                                  <div className="flex items-center gap-1">
                                    <Lock
                                      className="h-3 w-3"
                                      aria-label={t("hidden")}
                                      role="img"
                                    />
                                    <span>{t("hidden")}</span>
                                  </div>
                                </TableCell>
                                <TableCell className="text-right text-gray-600 dark:text-gray-400">
                                  {stats.private_aggregation.memory_count.toLocaleString()}
                                </TableCell>
                                <TableCell className="text-right text-gray-500 dark:text-gray-500">
                                  -
                                </TableCell>
                                <TableCell className="text-right text-gray-500 dark:text-gray-500">
                                  -
                                </TableCell>
                                <TableCell className="text-right text-gray-500 dark:text-gray-500">
                                  -
                                </TableCell>
                                <TableCell className="text-right text-gray-500 dark:text-gray-500">
                                  -
                                </TableCell>
                                <TableCell className="text-right text-gray-600 dark:text-gray-400">
                                  {stats.total_memories > 0
                                    ? (
                                        (stats.private_aggregation
                                          .memory_count /
                                          stats.total_memories) *
                                        100
                                      ).toFixed(1)
                                    : "0.0"}
                                  %
                                </TableCell>
                              </TableRow>
                            )}
                        </>
                      )}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
          )}

          {/* Memory Timeline - Issue #275 Task 6 */}
          {memoryTimeline && (
            <div className="mb-6">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                  <Calendar className="h-6 w-6" />
                  {t("memoryTimeline")}
                </h2>
                <div className="flex gap-2">
                  <Button
                    variant={timelineDays === 7 ? "default" : "outline"}
                    size="sm"
                    onClick={() => setTimelineDays(7)}
                  >
                    7 {t("days")}
                  </Button>
                  <Button
                    variant={timelineDays === 30 ? "default" : "outline"}
                    size="sm"
                    onClick={() => setTimelineDays(30)}
                  >
                    30 {t("days")}
                  </Button>
                </div>
              </div>

              <Card>
                <CardContent className="pt-6">
                  <ResponsiveContainer width="100%" height={300}>
                    <LineChart data={memoryTimeline.daily_counts}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis
                        dataKey="date"
                        tickFormatter={(date) => {
                          const d = new Date(date);
                          return `${d.getMonth() + 1}/${d.getDate()}`;
                        }}
                      />
                      <YAxis />
                      <Tooltip
                        labelFormatter={(date) =>
                          new Date(date).toLocaleDateString()
                        }
                      />
                      <Line
                        type="monotone"
                        dataKey="count"
                        stroke="#3b82f6"
                        strokeWidth={2}
                        name={t("memoriesCreated")}
                        dot={{ fill: "#3b82f6", r: 3 }}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </CardContent>
              </Card>
            </div>
          )}

          {/* Admin User Activity - Issue #275 Task 7, #134 uses global filter */}
          {(currentWorkspace?.current_user_role === "admin" ||
            currentWorkspace?.current_user_role === "owner") &&
            selectedContextId && (
              <div className="mb-6">
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
                      <Users className="h-6 w-6" />
                      {t("userActivity")}
                    </h2>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                      {t("perUserApiCallBreakdown")}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      variant={activityDays === 7 ? "default" : "outline"}
                      size="sm"
                      onClick={() => setActivityDays(7)}
                    >
                      7 {t("days")}
                    </Button>
                    <Button
                      variant={activityDays === 30 ? "default" : "outline"}
                      size="sm"
                      onClick={() => setActivityDays(30)}
                    >
                      30 {t("days")}
                    </Button>
                  </div>
                </div>

                <Card>
                  <CardContent className="pt-6">
                    {activityError ? (
                      <Alert variant="destructive" className="mb-4">
                        <AlertCircle className="h-4 w-4" />
                        <AlertTitle>{tCommon("error")}</AlertTitle>
                        <AlertDescription>{activityError}</AlertDescription>
                      </Alert>
                    ) : userActivity ? (
                      <Table>
                        <TableHeader>
                          <TableRow>
                            <TableHead>{t("userName")}</TableHead>
                            <TableHead>{t("email")}</TableHead>
                            <TableHead className="text-right">
                              {t("apiCalls")}
                            </TableHead>
                            <TableHead className="text-right">
                              {t("lastActivity")}
                            </TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {userActivity.users.length === 0 ? (
                            <TableRow>
                              <TableCell
                                colSpan={4}
                                className="text-center text-muted-foreground py-8"
                              >
                                {t("noUserActivity")}
                              </TableCell>
                            </TableRow>
                          ) : (
                            userActivity.users.map((user) => (
                              <TableRow key={user.user_id}>
                                <TableCell className="font-medium">
                                  {user.user_name || t("notAvailable")}
                                </TableCell>
                                <TableCell className="text-sm text-gray-600 dark:text-gray-400">
                                  {user.user_email || t("notAvailable")}
                                </TableCell>
                                <TableCell className="text-right">
                                  <span
                                    className={
                                      user.api_calls > 0
                                        ? "text-green-600 font-medium"
                                        : "text-gray-400"
                                    }
                                  >
                                    {user.api_calls.toLocaleString()}
                                  </span>
                                </TableCell>
                                <TableCell className="text-right text-sm text-gray-500">
                                  {user.last_activity
                                    ? formatDistanceToNow(
                                        new Date(user.last_activity),
                                        { addSuffix: true },
                                      )
                                    : "Never"}
                                </TableCell>
                              </TableRow>
                            ))
                          )}
                        </TableBody>
                      </Table>
                    ) : (
                      <div className="flex items-center justify-center py-8">
                        <InlineSpinner size="md" />
                        <span className="ml-3 text-slate-500">
                          {t("loadingUserActivity")}
                        </span>
                      </div>
                    )}
                  </CardContent>
                </Card>
              </div>
            )}

          {/* Workspace Usage Statistics */}
          <UsageStats scope="workspace" />

          {/* Per-member usage (Issue #331) */}
          <MemberUsageSection />
        </>
      ) : null}
    </PageContainer>
  );
}
