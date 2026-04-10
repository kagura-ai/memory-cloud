"use client";

import { useState, useMemo } from "react";
import { useTranslations, useLocale } from "next-intl";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Download, ArrowUpDown, Lock, Users, Eye, EyeOff } from "lucide-react";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { useAuth } from "@/contexts/AuthContext";
import Link from "next/link";
import type {
  ContextStatsResponse,
  ContextStatsItem,
  DashboardContextStats,
  PrivateContextAggregation,
} from "@/lib/api/workspaces";

interface ContextBreakdownTableProps {
  contexts: DashboardContextStats[];
  totalMemories: number;
  privateAggregation?: PrivateContextAggregation | null;
  contextStats: ContextStatsResponse | null;
  workspaceName?: string;
}

type SortColumn = "name" | "memory" | "activity";

function SortableHead({
  column,
  sortBy,
  onSort,
  children,
  className,
}: {
  column: SortColumn;
  sortBy: SortColumn;
  onSort: (column: SortColumn) => void;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <TableHead
      className={`cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800 ${className || ""}`}
      onClick={() => onSort(column)}
    >
      <div
        className={`flex items-center gap-1 ${className?.includes("text-right") ? "justify-end" : ""}`}
      >
        {children}
        {sortBy === column && <ArrowUpDown className="h-3 w-3" />}
      </div>
    </TableHead>
  );
}

export function ContextBreakdownTable({
  contexts,
  totalMemories,
  privateAggregation,
  contextStats,
  workspaceName,
}: ContextBreakdownTableProps) {
  const t = useTranslations("workspace");
  const tDashboard = useTranslations("dashboard");
  const { user: authUser } = useAuth();
  const locale = useLocale();

  const [sortBy, setSortBy] = useState<SortColumn>("memory");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [showDetails, setShowDetails] = useState(false);

  const handleSort = (column: SortColumn) => {
    if (sortBy === column) {
      setSortOrder(sortOrder === "asc" ? "desc" : "asc");
    } else {
      setSortBy(column);
      setSortOrder("desc");
    }
  };

  const contextDetailMap = useMemo(
    () => new Map(contextStats?.contexts.map((c) => [c.context_id, c]) ?? []),
    [contextStats],
  );

  const sortedContexts = useMemo(() => {
    return [...contexts].sort((a, b) => {
      const aDetail = contextDetailMap.get(a.context_id);
      const bDetail = contextDetailMap.get(b.context_id);

      let aValue: string | number;
      let bValue: string | number;

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
    });
  }, [contexts, contextDetailMap, sortBy, sortOrder]);

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
    a.download = `context-stats-${workspaceName?.toLowerCase().replace(/\s+/g, "-") || "export"}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
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
        <div className="flex items-center gap-2">
          <Button
            onClick={() => setShowDetails(!showDetails)}
            variant="outline"
            size="sm"
          >
            {showDetails ? (
              <EyeOff className="h-4 w-4 mr-2" />
            ) : (
              <Eye className="h-4 w-4 mr-2" />
            )}
            {tDashboard(showDetails ? "hideDetails" : "showDetails")}
          </Button>
          <Button
            onClick={exportToCSV}
            variant="outline"
            size="sm"
            disabled={!contextStats}
          >
            <Download className="h-4 w-4 mr-2" />
            Export CSV
          </Button>
        </div>
      </div>

      <Card>
        <CardContent className="pt-6">
          <Table>
            <TableHeader>
              <TableRow>
                <SortableHead column="name" sortBy={sortBy} onSort={handleSort}>
                  {t("contextName")}
                </SortableHead>
                <SortableHead
                  column="memory"
                  sortBy={sortBy}
                  onSort={handleSort}
                  className="text-right"
                >
                  {t("memoriesCount")}
                </SortableHead>
                <SortableHead
                  column="activity"
                  sortBy={sortBy}
                  onSort={handleSort}
                  className="text-right"
                >
                  {t("lastActivity")}
                </SortableHead>
                {showDetails && (
                  <>
                    <TableHead>{t("owner")}</TableHead>
                    <TableHead className="text-right">
                      {t("apiCallsWeek")}
                    </TableHead>
                    <TableHead className="text-right">
                      {t("activeUsersWeek")}
                    </TableHead>
                    <TableHead className="text-right">{t("members")}</TableHead>
                    <TableHead className="text-right">
                      {t("percentOfTotal")}
                    </TableHead>
                  </>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {contexts.length === 0 &&
              (!privateAggregation ||
                privateAggregation.context_count === 0) ? (
                <TableRow>
                  <TableCell
                    colSpan={showDetails ? 8 : 3}
                    className="text-center text-muted-foreground py-8"
                  >
                    {t("noContextsFound")}
                  </TableCell>
                </TableRow>
              ) : (
                <>
                  {sortedContexts.map((context) => {
                    const percentage =
                      totalMemories > 0
                        ? (
                            (context.memory_count / totalMemories) *
                            100
                          ).toFixed(1)
                        : "0.0";
                    const contextDetail = contextDetailMap.get(
                      context.context_id,
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
                              href={`/workspace/contexts/${context.context_id}`}
                              className="text-indigo-600 hover:text-indigo-900 dark:text-indigo-400 dark:hover:text-indigo-300 truncate max-w-[200px]"
                            >
                              {context.context_name}
                            </Link>
                          </div>
                        </TableCell>
                        <TableCell className="text-right">
                          {context.memory_count.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right text-sm text-gray-500">
                          {contextDetail?.last_activity
                            ? formatRelativeTime(
                                contextDetail.last_activity,
                                authUser?.timezone,
                                locale,
                              )
                            : "Never"}
                        </TableCell>
                        {showDetails && (
                          <>
                            <TableCell className="text-sm text-gray-600 dark:text-gray-400">
                              {context.created_by_name ||
                                context.created_by ||
                                t("notAvailable")}
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
                                {contextDetail?.active_users_week?.toLocaleString() ??
                                  "0"}
                              </span>
                            </TableCell>
                            <TableCell className="text-right">
                              {contextDetail?.member_count?.toLocaleString() ??
                                "-"}
                            </TableCell>
                            <TableCell className="text-right">
                              {percentage}%
                            </TableCell>
                          </>
                        )}
                      </TableRow>
                    );
                  })}

                  {privateAggregation &&
                    privateAggregation.context_count > 0 && (
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
                                count: privateAggregation.context_count,
                              })}
                            </span>
                          </div>
                        </TableCell>
                        <TableCell className="text-right text-gray-600 dark:text-gray-400">
                          {privateAggregation.memory_count.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-right text-gray-500 dark:text-gray-500">
                          -
                        </TableCell>
                        {showDetails && (
                          <>
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
                              {totalMemories > 0
                                ? (
                                    (privateAggregation.memory_count /
                                      totalMemories) *
                                    100
                                  ).toFixed(1)
                                : "0.0"}
                              %
                            </TableCell>
                          </>
                        )}
                      </TableRow>
                    )}
                </>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
