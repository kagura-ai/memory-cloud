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
  useState,
  useRef,
  useCallback,
  forwardRef,
  useImperativeHandle,
} from "react";
import { useRouter } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Progress } from "@/components/ui/progress";
import { Brain, Zap, TrendingUp, XCircle, Moon, Briefcase } from "lucide-react";
import type {
  PlanLimits,
  QuotaUsage,
  SleepContextsUsage,
  WorkspacesUsage,
} from "@/lib/api/usage";
import {
  getWorkspaceUsageCurrent,
} from "@/lib/api/workspaces";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { isPlanTier, planLabelFromEnv } from "@/lib/utils/planLabel";
import { QuotaOverview } from "./QuotaOverview";
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
  memories_created_today?: number; // Issue #1549 — optional (additive field)
  sleep_contexts: SleepContextsUsage | null; // Issue #560
  workspaces: WorkspacesUsage; // Issue #661 — always populated
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
  quota_usage?: QuotaUsage[];
  plan: PlanLimits;
  usage: CurrentUsage;
  memory_usage: UsageStatus;
  daily_api_usage: UsageStatus;
  weekly_api_usage: UsageStatus;
}

export interface UsageStatsRef {
  refresh: () => Promise<void>;
}

export interface UsageStatsProps {
  className?: string;
  onQuotasLoaded?: (workspaceId: string, meters: QuotaUsage[]) => void;
}

export const UsageStats = forwardRef<UsageStatsRef, UsageStatsProps>(
  ({ className, onQuotasLoaded }, ref) => {
    const t = useTranslations("usageStats");

    const { currentWorkspaceId } = useWorkspace();
    const locale = useLocale();
    const router = useRouter();
    const [currentUsage, setCurrentUsage] = useState<UsageCurrentData | null>(
      null,
    );
    const [isLoading, setIsLoading] = useState(true);
    const requestId = useRef(0);
    const cancelPending = useCallback(() => { requestId.current++; }, []);
    const [loadedWorkspaceId, setLoadedWorkspaceId] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);

    const fetchAllStats = async () => {
      const request = ++requestId.current;
      const workspaceId = currentWorkspaceId;
      try {
        setIsLoading(true);

        const current = await getWorkspaceUsageCurrent();

        if (request !== requestId.current) return;
        setLoadedWorkspaceId(workspaceId);
        if (workspaceId) onQuotasLoaded?.(workspaceId, current.quota_usage ?? []);
        setCurrentUsage(current);
        setError(null);
      } catch (err) {
        if (request !== requestId.current) return;
        console.error("Failed to fetch usage stats:", err);
        setError(err instanceof Error ? err.message : t("failedToLoad"));
      } finally {
        if (request === requestId.current) setIsLoading(false);
      }
    };

    // Expose refresh function to parent via ref
    useImperativeHandle(ref, () => ({
      refresh: fetchAllStats,
    }));

    useEffect(() => {
      // Re-fetch when the active workspace changes.
      if (currentWorkspaceId !== null) {
        fetchAllStats();
      }
      return cancelPending;
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [currentWorkspaceId]);

    if (isLoading || (!error && loadedWorkspaceId !== currentWorkspaceId)) {
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

    if (error || !currentUsage) {
      return (
        <Alert variant="destructive">
          <XCircle className="h-4 w-4" />
          <AlertDescription>{error || t("failedToLoad")}</AlertDescription>
        </Alert>
      );
    }

    // Issue #1549: "created today" rides the Memories card. Both fields are
    // additive on the backend, so render only when the payload carries the
    // count and the tier actually allows creation (0 = zero-floor, not
    // unlimited — showing "n / 0" would read as a bug).
    const memoriesCreatedToday = currentUsage.usage.memories_created_today;
    const memoriesPerDay = currentUsage.plan.memories_per_day ?? 0;
    const showMemoriesToday =
      typeof memoriesCreatedToday === "number" && memoriesPerDay > 0;

    return (
      <div className={"space-y-6 " + (className ?? "")}>
        {currentUsage.quota_usage?.length ? (
          <QuotaOverview meters={currentUsage.quota_usage} />
        ) : (
          <>
        {/* Quota Warnings - Issue #149 */}
        <QuotaWarning
          current={currentUsage.usage.memory_count}
          limit={currentUsage.plan.memory_limit}
          resourceLabel={t("memories")}
          onUpgrade={() => router.push("/workspace/settings/plan")}
        />

        {/* Current Usage Cards */}
        <div>
          <h2 className="text-2xl font-bold mb-4">
            {t("usage")} -{" "}
            {isPlanTier(currentUsage.plan.plan_name)
              ? planLabelFromEnv(currentUsage.plan.plan_name, locale)
              : currentUsage.plan.plan_name}{" "}
            {t("plan")}
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
                {showMemoriesToday && (
                  <p className="text-xs text-muted-foreground mt-1">
                    {t("memoriesCreatedToday", {
                      count: memoriesCreatedToday,
                      limit: memoriesPerDay,
                    })}
                  </p>
                )}
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
                sleep_contexts which is workspace-scoped. Always populated
                by the backend so this card renders unconditionally. */}
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

          </>
        )}

      </div>
    );
  },
);

UsageStats.displayName = "UsageStats";
