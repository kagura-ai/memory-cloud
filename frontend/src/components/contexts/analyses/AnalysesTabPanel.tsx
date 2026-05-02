"use client";

/**
 * AnalysesTabPanel — orchestrator for the analyses tab (Issue #497).
 *
 * Owns:
 *  - Active run loading (``getActiveAnalysis``).
 *  - Cluster list + positions for the active run (``listRunClusters`` /
 *    ``listRunPositions``), gated to only succeed when there IS an
 *    active succeeded run.
 *  - Past run history (``listAnalysisRuns``).
 *  - URL-driven modal trigger (``?new=1``).
 *  - URL-synced focused cluster state via ``useFocusedClusterId``.
 *  - Active in-flight run polling (``useActiveAnalysisPolling``) when a
 *    new run was just started or one was already running on mount.
 *
 * Allowlist 403: the API gate returns 403 when the workspace is not on
 * the allowlist. We render a friendly empty state — flat copy that
 * does not reveal the allowlist exists (CDO advice from gate1).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ApiError } from "@/lib/api/base";
import {
  cancelAnalysisRun,
  getActiveAnalysis,
  listAnalysisRuns,
  listRunClusters,
  listRunPositions,
  type AnalysisCluster,
  type AnalysisRunRow,
  type ScatterPosition,
} from "@/lib/api/analyses";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import {
  CardLoadingState,
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { KpiCard } from "@/components/ui/kpi-card";
import { Button } from "@/components/ui/button";
import {
  Lock,
  Plus,
  Sparkles,
  Activity,
  DollarSign,
  Award,
  Clock,
} from "lucide-react";
import { ScatterPlot } from "./ScatterPlot";
import { ClusterList } from "./ClusterList";
import { RepresentativesPanel } from "./RepresentativesPanel";
import { PropertyStats } from "./PropertyStats";
import { AnalysisHistory } from "./AnalysisHistory";
import { NewAnalysisModal } from "./NewAnalysisModal";
import { useFocusedClusterId } from "./useFocusedClusterId";
import { useActiveAnalysisPolling } from "./useActiveAnalysisPolling";
import { formatCostCents, formatConfidence } from "./analysisFormatters";

interface AnalysesTabPanelProps {
  contextId: string;
  contextName: string;
}

interface BootstrapState {
  activeRun: AnalysisRunRow | null;
  clusters: AnalysisCluster[];
  positions: ScatterPosition[];
  history: AnalysisRunRow[];
  error: string | null;
  notEnabled: boolean;
  loading: boolean;
}

const EMPTY_BOOTSTRAP: BootstrapState = {
  activeRun: null,
  clusters: [],
  positions: [],
  history: [],
  error: null,
  notEnabled: false,
  loading: true,
};

export function AnalysesTabPanel({
  contextId,
  contextName,
}: AnalysesTabPanelProps) {
  const t = useTranslations("analyses");
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const [state, setState] = useState<BootstrapState>(EMPTY_BOOTSTRAP);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  // Modal state is URL-driven (?new=1). Reading + writing the URL is
  // the single source of truth so back/forward keeps the modal closed
  // / open consistently.
  const showModal = searchParams.get("new") === "1";

  const setShowModal = useCallback(
    (next: boolean) => {
      const params = new URLSearchParams(searchParams.toString());
      if (next) params.set("new", "1");
      else params.delete("new");
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname);
    },
    [searchParams, pathname, router],
  );

  // Bootstrap: active + clusters + positions + history. Errors fall
  // into the friendly empty-state path on 403 (allowlist) or
  // "no run yet" path on 404 from /active.
  const bootstrap = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));

    let activeRun: AnalysisRunRow | null = null;
    let clusters: AnalysisCluster[] = [];
    let positions: ScatterPosition[] = [];
    let history: AnalysisRunRow[] = [];

    // History is independent of active-run resolution — fetch in
    // parallel so the user sees past runs even when the active call
    // 404s. ``.catch`` swallows non-403 errors directly (no rethrow)
    // so the early-return notEnabled path doesn't abandon a rejected
    // promise. 403 is treated as the allowlist signal AND triggers
    // the same notEnabled state below.
    let historyAllowlistDenied = false;
    const historyPromise = listAnalysisRuns(contextId, { limit: 12 }).catch(
      (err) => {
        if (err instanceof ApiError && err.status === 403) {
          historyAllowlistDenied = true;
        }
        return { items: [], next_cursor: null };
      },
    );

    try {
      try {
        activeRun = await getActiveAnalysis(contextId);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          activeRun = null;
        } else if (err instanceof ApiError && err.status === 403) {
          // Even on early notEnabled return, drain the in-flight
          // history promise so we don't abandon a rejected promise
          // and surface an unhandled-rejection warning.
          await historyPromise;
          setState({
            ...EMPTY_BOOTSTRAP,
            notEnabled: true,
            loading: false,
          });
          return;
        } else {
          throw err;
        }
      }

      if (activeRun) {
        const [clusterRes, positionRes] = await Promise.all([
          listRunClusters(contextId, activeRun.run_id),
          listRunPositions(contextId, activeRun.run_id),
        ]);
        clusters = clusterRes.items;
        positions = positionRes.items;
      }

      const historyRes = await historyPromise;
      history = historyRes.items;
      // ``getActiveAnalysis`` only returns the most-recent SUCCEEDED
      // run, so a run still in flight (e.g. started from another
      // session, or the page was refreshed mid-run) would otherwise
      // be invisible to the polling hook. Scan the history page for a
      // running row and prefer it as the polling target so the banner
      // + Cancel button surface even after a refresh. The succeeded
      // run from ``activeRun`` continues to drive the scatter view
      // (clusters / positions).
      const inFlight = history.find((r) => r.status === "running") ?? null;
      setActiveRunId(inFlight?.run_id ?? activeRun?.run_id ?? null);
      if (historyAllowlistDenied && !activeRun) {
        // Allowlist denied AND no active run — render the friendly
        // notEnabled empty state. (When activeRun exists from a cached
        // succeeded run, the panel still shows the run; the history
        // list is just empty.)
        setState({
          ...EMPTY_BOOTSTRAP,
          notEnabled: true,
          loading: false,
        });
        return;
      }

      setState({
        activeRun,
        clusters,
        positions,
        history,
        error: null,
        notEnabled: false,
        loading: false,
      });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setState({
          ...EMPTY_BOOTSTRAP,
          notEnabled: true,
          loading: false,
        });
        return;
      }
      setState({
        ...EMPTY_BOOTSTRAP,
        error: err instanceof Error ? err.message : t("states.loadFailed"),
        loading: false,
      });
    }
  }, [contextId, t]);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  // Polling: armed only when activeRunId points at a *running* run.
  // The terminal callback re-runs bootstrap so the cluster+position
  // payload of the just-finished run lands.
  const pollingRunId = useMemo(() => {
    if (!activeRunId) return null;
    // Only poll the active run id when it's *known* to be running.
    // For an already-succeeded ``getActiveAnalysis`` result, polling
    // would burn quota for nothing.
    if (
      state.activeRun &&
      state.activeRun.run_id === activeRunId &&
      state.activeRun.status !== "running"
    ) {
      return null;
    }
    return activeRunId;
  }, [activeRunId, state.activeRun]);

  const polling = useActiveAnalysisPolling({
    contextId,
    runId: pollingRunId,
    onTerminal: () => {
      bootstrap();
    },
  });

  const handleCancelRun = useCallback(async () => {
    if (!polling.run || polling.run.status !== "running") return;
    try {
      await cancelAnalysisRun(contextId, polling.run.run_id);
      polling.refetch();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn("cancelAnalysisRun failed", err);
    }
  }, [contextId, polling]);

  const allowedClusterIndexes = useMemo(
    () => state.clusters.map((c) => c.cluster_index),
    [state.clusters],
  );
  const { focusedClusterId, setFocusedClusterId, toggleFocusedClusterId } =
    useFocusedClusterId(allowedClusterIndexes);

  // ``focusedCluster`` is null when no cluster is focused — the
  // RepresentativesPanel renders an "all clusters" empty state instead
  // of silently surfacing the largest cluster's reps and burning
  // ``referenceMemory`` calls before the user has chosen anything.
  const focusedCluster = useMemo(() => {
    if (focusedClusterId === null) return null;
    return (
      state.clusters.find((c) => c.cluster_index === focusedClusterId) ?? null
    );
  }, [focusedClusterId, state.clusters]);

  const handleRunStarted = useCallback(
    (runId: string) => {
      setActiveRunId(runId);
      setShowModal(false);
      // bootstrap() will be re-triggered by the polling onTerminal
      // when the run finishes; until then the polling hook displays
      // the in-flight status.
    },
    [setShowModal],
  );

  // ----- render -----

  if (state.loading) {
    // Skeleton stack instead of a spinner — preserves layout (no CLS)
    // and matches the rule in .claude/rules/frontend.md ("Prefer
    // skeletons over spinners for first paint").
    return (
      <div className="space-y-6" role="status" aria-label={t("states.loading")}>
        <CardLoadingState count={4} />
        <TableLoadingState rows={6} />
      </div>
    );
  }

  if (state.notEnabled) {
    return (
      <EmptyState
        icon={Lock}
        title={t("states.notEnabled.title")}
        description={t("states.notEnabled.description")}
      />
    );
  }

  if (state.error) {
    return <ErrorBanner error={state.error} />;
  }

  const runInProgress =
    polling.run && polling.run.status === "running" ? polling.run : null;

  return (
    <div className="space-y-6">
      {/* top action row */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {t("header.title")}
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {t("header.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={() => setShowModal(true)}
            disabled={!!runInProgress}
            className="gap-1.5"
          >
            <Plus className="h-3.5 w-3.5" />
            {t("actions.newAnalysis")}
          </Button>
        </div>
      </div>

      {/* Running banner — visible while a run is in progress so the
          page does not look frozen during the (possibly minutes-long)
          LLM labeling stage. Cancel button soft-cancels via DELETE. */}
      {runInProgress && (
        <div className="flex items-center justify-between gap-4 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 dark:border-blue-800 dark:bg-blue-900/30">
          <div className="flex items-center gap-3">
            <InlineSpinner size="sm" variant="brand" />
            <div className="text-sm">
              <div className="font-medium text-gray-900 dark:text-gray-100">
                {t("running.title")}
              </div>
              <div className="text-xs text-gray-600 dark:text-gray-400">
                {t("running.startedAt", {
                  when: new Date(runInProgress.started_at).toLocaleString(),
                })}
              </div>
            </div>
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={handleCancelRun}
            className="shrink-0"
          >
            {t("running.cancel")}
          </Button>
        </div>
      )}

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <KpiCard
          icon={Clock}
          label={t("kpi.lastRun")}
          value={
            state.activeRun
              ? new Date(state.activeRun.started_at).toLocaleString()
              : t("kpi.neverRun")
          }
          tone={state.activeRun ? "primary" : "muted"}
        />
        <KpiCard
          icon={Activity}
          label={t("kpi.memoriesSurveyed")}
          value={state.activeRun ? state.activeRun.input_count : "—"}
        />
        <KpiCard
          icon={DollarSign}
          label={t("kpi.runCost")}
          value={
            state.activeRun
              ? formatCostCents(
                  state.activeRun.cost_actual_cents ??
                    state.activeRun.cost_estimated_cents,
                )
              : "—"
          }
        />
        <KpiCard
          icon={Award}
          label={t("kpi.quality")}
          value={
            state.clusters.length === 0
              ? t("kpi.qualityNoData")
              : t("kpi.qualityValue", {
                  value: formatConfidence(
                    state.clusters
                      .map((c) => c.label_confidence)
                      .reduce((acc, n) => acc + n, 0) / state.clusters.length,
                  ),
                })
          }
          tone={state.clusters.length === 0 ? "muted" : "secondary"}
        />
      </div>

      {/* main scatter + cluster list grid */}
      {state.clusters.length === 0 ? (
        <EmptyState
          icon={Sparkles}
          title={t("states.empty.title")}
          description={t("states.empty.description")}
          // Hide the action button while a run is in flight — the
          // running banner above already provides the visual signal
          // and a Cancel control. Re-enabling on terminal lets the
          // user fire a follow-up run if the first one failed.
          actionLabel={runInProgress ? undefined : t("actions.newAnalysis")}
          onAction={runInProgress ? undefined : () => setShowModal(true)}
        />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
            <div className="space-y-4">
              <ScatterPlot
                clusters={state.clusters}
                positions={state.positions}
                focusedClusterId={focusedClusterId}
                focusedCluster={focusedCluster}
                onClearFocus={() => setFocusedClusterId(null)}
              />
              <RepresentativesPanel
                cluster={focusedCluster}
                totalCount={focusedCluster?.count ?? 0}
              />
            </div>
            <ClusterList
              clusters={state.clusters}
              focusedClusterId={focusedClusterId}
              onToggleFocus={toggleFocusedClusterId}
              onClearFocus={() => setFocusedClusterId(null)}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <AnalysisHistory
              runs={state.history}
              total={state.history.length}
              activeRunId={state.activeRun?.run_id ?? null}
            />
            <PropertyStats
              clusters={state.clusters}
              focusedCluster={focusedCluster}
            />
          </div>
        </>
      )}

      <NewAnalysisModal
        open={showModal}
        contextId={contextId}
        contextName={contextName}
        onClose={() => setShowModal(false)}
        onStarted={handleRunStarted}
      />
    </div>
  );
}
