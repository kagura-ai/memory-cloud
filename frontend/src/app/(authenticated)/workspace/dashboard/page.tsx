"use client";

/**
 * Workspace Dashboard Page
 *
 * Issue #115 - Workspace-level Multi-tenancy Support
 * Issue #234 - Progressive disclosure (KPI above fold + collapsible admin)
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { hasWorkspaceRole } from "@/lib/auth/rbac";
import { PageContainer } from "@/components/common/PageContainer";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { RefreshCw, AlertCircle } from "lucide-react";
import { apiClient } from "@/lib/api/base";
import { InlineSpinner } from "@/components/common/LoadingState";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { UsageStats } from "@/components/dashboard/UsageStats";
import { KpiCards } from "@/components/dashboard/KpiCards";
import { ContextBreakdownTable } from "@/components/dashboard/ContextBreakdownTable";
import { MemoryTimelineChart } from "@/components/dashboard/MemoryTimelineChart";
import { AdminSections } from "@/components/dashboard/AdminSections";
import { PlanBadge } from "@/components/common/PlanBadge";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import {
  clearRecentlyDeletedWorkspace,
  readRecentlyDeletedWorkspace,
} from "@/lib/storage/recently-deleted-workspace";
import {
  getContextStats,
  ContextStatsResponse,
  getWorkspaceMemoryTimeline,
  MemoryTimelineResponse,
  WorkspaceStats,
} from "@/lib/api/workspaces";

// 10-minute window between user-clicked-delete and dashboard mount. Long
// enough to span a 2FA round-trip; short enough that stale stash from a
// previous session doesn't fire a misleading toast.
const AUTO_SWITCH_TOAST_TTL_MS = 10 * 60 * 1000;

export default function WorkspaceStatsPage() {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const router = useRouter();
  const {
    currentWorkspace,
    currentWorkspaceId,
    loading: workspaceLoading,
  } = useWorkspace();
  const { toast } = useToast();
  const { user } = useAuth();

  // Issue #398: viewer cannot read workspace stats (backend 403's on
  // /workspaces/{id}/contexts/stats with required_role="member"). Send
  // viewers to the contexts list — their only data-bearing surface.
  useEffect(() => {
    if (
      currentWorkspace &&
      !hasWorkspaceRole(currentWorkspace.current_user_role, "member")
    ) {
      router.push("/workspace/contexts");
    }
  }, [currentWorkspace, router]);

  // Issue #660: surface backend auto-switch as a one-shot toast. The settings
  // page stashed the deleted workspace's name in localStorage just before
  // `deleteWorkspace()`; the backend has since picked a remaining workspace via
  // role-preferring order. Read once, render `"<old> was deleted — switched to
  // <new>"`, then clear.
  useEffect(() => {
    if (!currentWorkspace || !currentWorkspaceId || !user?.id) return;
    const stash = readRecentlyDeletedWorkspace();
    if (!stash) return;

    const now = Date.now();
    // Clamp future-dated `ts` (tampering / clock skew) — without the upper
    // bound, a future timestamp passes `now - ts < TTL` indefinitely.
    const isRecent =
      stash.ts <= now && now - stash.ts < AUTO_SWITCH_TOAST_TTL_MS;
    const isDifferentWorkspace = stash.id !== currentWorkspaceId;
    // Bind to the authenticated user: localStorage is per-origin, not per-user,
    // so account A's stash must NOT surface in account B's toast after a
    // logout/login on the same browser (Copilot review on PR #662). When the
    // stash belongs to a different user, drop it without rendering.
    const isSameUser = stash.user_id === user.id;

    if (isSameUser && isRecent && isDifferentWorkspace) {
      toast({
        title: t("workspaceAutoSwitchedTitle"),
        description: t("workspaceAutoSwitchedDesc", {
          deleted: stash.name,
          switched: currentWorkspace.name,
        }),
      });
    }
    clearRecentlyDeletedWorkspace();
  }, [currentWorkspace, currentWorkspaceId, user?.id, t, toast]);
  const [stats, setStats] = useState<WorkspaceStats | null>(null);
  const [contextStats, setContextStats] = useState<ContextStatsResponse | null>(
    null,
  );
  const [memoryTimeline, setMemoryTimeline] =
    useState<MemoryTimelineResponse | null>(null);
  const [timelineDays, setTimelineDays] = useState<7 | 30>(30);
  const [selectedContextId, setSelectedContextId] = useState<string | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchStats = async () => {
    try {
      setLoading(true);
      setError(null);
      const [statsResponse, contextStatsResponse] = await Promise.all([
        apiClient.get<WorkspaceStats>("/api/v1/workspace/stats"),
        currentWorkspaceId
          ? getContextStats(currentWorkspaceId)
          : Promise.resolve(null),
      ]);
      setStats(statsResponse);
      setContextStats(contextStatsResponse);
    } catch (err: unknown) {
      console.error("Failed to fetch workspace stats:", err);
      setError(err instanceof Error ? err.message : t("failedToLoadStats"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // Skip the protected fetches for viewer — the redirect useEffect above
    // is sending them to /workspace/contexts, and the backend (workspace
    // stats + contexts/stats) returns 403 for viewer. Without this guard a
    // viewer flashes a "Requires 'member' role or higher" error toast in
    // the gap between login and the redirect.
    //
    // The workspaceLoading guard is load-bearing: during hydration
    // currentWorkspace is null while currentWorkspaceId may already be
    // cached. Without it the viewer-skip is bypassed and fetchStats fires
    // once with unknown role, recreating the flash this code prevents.
    if (workspaceLoading) return;
    if (
      currentWorkspace &&
      !hasWorkspaceRole(currentWorkspace.current_user_role, "member")
    ) {
      return;
    }
    fetchStats();
  }, [
    currentWorkspaceId,
    currentWorkspace?.current_user_role,
    workspaceLoading,
  ]);

  useEffect(() => {
    if (!currentWorkspaceId) return;
    if (workspaceLoading) return;
    // Same viewer-skip as above — memory-timeline is a member+ surface.
    if (
      currentWorkspace &&
      !hasWorkspaceRole(currentWorkspace.current_user_role, "member")
    ) {
      return;
    }

    const controller = new AbortController();
    getWorkspaceMemoryTimeline(
      currentWorkspaceId,
      timelineDays,
      selectedContextId || undefined,
    )
      .then((data) => {
        if (!controller.signal.aborted) setMemoryTimeline(data);
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          console.error("Failed to load memory timeline:", err);
          setMemoryTimeline(null);
        }
      });

    return () => controller.abort();
  }, [
    timelineDays,
    currentWorkspaceId,
    selectedContextId,
    currentWorkspace?.current_user_role,
    workspaceLoading,
  ]);

  return (
    <PageContainer>
      <div className="flex items-center justify-between mb-6">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-3xl font-bold">{t("overview")}</h1>
            {stats && (
              <PlanBadge
                planName={stats.plan_name as "free" | "basic" | "pro"}
                size="sm"
                className="translate-y-0.5"
              />
            )}
          </div>
          <p className="text-muted-foreground mt-1">
            {currentWorkspace?.description || t("overviewDesc")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {stats?.contexts && stats.contexts.length > 0 && (
            <Select
              value={selectedContextId || "all"}
              onValueChange={(value) =>
                setSelectedContextId(value === "all" ? null : value)
              }
            >
              <SelectTrigger
                className="w-[200px]"
                aria-label={t("filterByContext")}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t("allContexts")}</SelectItem>
                {stats.contexts.map((ctx) => (
                  <SelectItem key={ctx.context_id} value={ctx.context_id}>
                    {ctx.context_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button
            onClick={fetchStats}
            variant="outline"
            size="sm"
            disabled={loading}
          >
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
          <KpiCards
            totalMemories={stats.total_memories}
            contextCount={stats.context_count}
            contextStats={contextStats}
          />

          {memoryTimeline && (
            <MemoryTimelineChart
              timeline={memoryTimeline}
              days={timelineDays}
              onDaysChange={setTimelineDays}
            />
          )}

          {!selectedContextId && (
            <ContextBreakdownTable
              contexts={stats.contexts}
              totalMemories={stats.total_memories}
              privateAggregation={stats.private_aggregation}
              contextStats={contextStats}
              workspaceName={currentWorkspace?.name}
            />
          )}

          <AdminSections
            selectedContextId={selectedContextId}
            currentWorkspaceId={currentWorkspaceId}
          />

          <UsageStats scope="workspace" />
        </>
      ) : null}
    </PageContainer>
  );
}
