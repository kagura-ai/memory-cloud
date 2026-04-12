"use client";

/**
 * GraphTabPanel — Bounded neural graph visualization.
 *
 * Renders a d3-force based force-directed layout of the top-N nodes from the
 * context's neural memory graph. Gated by NEXT_PUBLIC_ENABLE_GRAPH_VIZ env var
 * at the page level (this component doesn't check the flag itself).
 *
 * Issue #233 — bounded neural graph visualization.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { graphApi } from "@/lib/api/graph";
import type { GraphData, GraphNode } from "@/lib/types/graph";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import { Brain } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { applyFilter, type FilterStrategy } from "@/lib/graph/nodeFilter";
import {
  FORCE_PRESETS,
  PRESET_NAMES,
  type PresetName,
} from "@/lib/graph/forcePresets";
import { useForceSimulation } from "@/hooks/useForceSimulation";

interface GraphTabPanelProps {
  contextId: string;
}

const N_OPTIONS: readonly number[] = [10, 25, 50];
const STRATEGY_OPTIONS: readonly FilterStrategy[] = [
  "degree",
  "importance",
  "weightSum",
];

// CSS-var backed colors — auto-inverts in dark mode via --chart-* vars
const TYPE_TO_CHART: Record<string, string> = {
  code: "hsl(var(--chart-1))",
  note: "hsl(var(--chart-2))",
  decision: "hsl(var(--chart-3))",
  error: "hsl(var(--chart-4))",
  feature: "hsl(var(--chart-5))",
  bug: "hsl(var(--chart-4))",
  refactor: "hsl(var(--chart-1))",
  test: "hsl(var(--chart-2))",
  docs: "hsl(var(--chart-3))",
  unknown: "hsl(var(--muted-foreground))",
};

function colorForNode(node: GraphNode): string {
  const key = node.type?.toLowerCase() ?? "unknown";
  return TYPE_TO_CHART[key] ?? TYPE_TO_CHART.unknown;
}

export function GraphTabPanel({ contextId }: GraphTabPanelProps) {
  const t = useTranslations("contexts");

  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState<number>(50);
  const [strategy, setStrategy] = useState<FilterStrategy>("degree");
  const [preset, setPreset] = useState<PresetName>("default");
  const [hovered, setHovered] = useState<GraphNode | null>(null);
  const [dims, setDims] = useState<{ w: number; h: number }>({
    w: 600,
    h: 400,
  });

  const svgRef = useRef<SVGSVGElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // --- Data fetch ---
  const fetchData = useCallback(async () => {
    if (!contextId) return;
    setLoading(true);
    setError(null);
    try {
      const result = await graphApi.getGraphData(contextId, {
        limit_nodes: 200,
      });
      setGraphData(result);
    } catch (err) {
      let message = t("graphVizLoadError");
      if (err !== null && typeof err === "object" && "message" in err) {
        const m = (err as { message: unknown }).message;
        if (typeof m === "string") message = m;
      }
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [contextId, t]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // --- Responsive dims ---
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        setDims({ w: Math.max(200, width), h: Math.max(200, height) });
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // --- Filtered subset ---
  const filtered = useMemo(() => {
    if (!graphData) return { nodes: [] as GraphNode[], edges: [] };
    return applyFilter({
      nodes: graphData.nodes,
      edges: graphData.edges,
      n,
      strategy,
    });
  }, [graphData, n, strategy]);

  // --- Type counts for legend ---
  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of filtered.nodes) {
      const key = node.type?.toLowerCase() ?? "unknown";
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([type, count]) => ({
        type,
        count,
        color: TYPE_TO_CHART[type] ?? TYPE_TO_CHART.unknown,
      }));
  }, [filtered.nodes]);

  // Clear stale hover when the filtered set changes (SVG is rebuilt)
  useEffect(() => {
    setHovered(null);
  }, [filtered]);

  // --- Force simulation (auto-starts) ---
  useForceSimulation({
    nodes: filtered.nodes,
    edges: filtered.edges,
    preset: FORCE_PRESETS[preset],
    width: dims.w,
    height: dims.h,
    svgRef,
    onHoverChange: setHovered,
    colorForNode,
  });

  if (loading) {
    return <SpinnerLoading size="lg" message={t("graphVizLoading")} />;
  }

  if (error) {
    return <ErrorBanner error={error} />;
  }

  if (!graphData || graphData.nodes.length === 0) {
    return (
      <EmptyState
        icon={Brain}
        title={t("graphEmptyTitle")}
        description={t("graphEmpty")}
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* Control strip */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 p-4 rounded-lg border border-slate-200 dark:border-slate-800">
        <div className="flex flex-col gap-1 text-xs">
          <label
            htmlFor="graph-viz-n"
            className="font-medium text-slate-700 dark:text-slate-300"
          >
            {t("graphVizNodeCount")}
          </label>
          <Select value={String(n)} onValueChange={(v) => setN(Number(v))}>
            <SelectTrigger id="graph-viz-n">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {N_OPTIONS.map((opt) => (
                <SelectItem key={opt} value={String(opt)}>
                  {opt}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1 text-xs">
          <label
            htmlFor="graph-viz-strategy"
            className="font-medium text-slate-700 dark:text-slate-300"
          >
            {t("graphVizStrategy")}
          </label>
          <Select
            value={strategy}
            onValueChange={(v) => setStrategy(v as FilterStrategy)}
          >
            <SelectTrigger id="graph-viz-strategy">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {STRATEGY_OPTIONS.map((opt) => (
                <SelectItem key={opt} value={opt}>
                  {t(`graphVizStrategy_${opt}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1 text-xs">
          <label
            htmlFor="graph-viz-preset"
            className="font-medium text-slate-700 dark:text-slate-300"
          >
            {t("graphVizPreset")}
          </label>
          <Select
            value={preset}
            onValueChange={(v) => setPreset(v as PresetName)}
          >
            <SelectTrigger id="graph-viz-preset">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PRESET_NAMES.map((opt) => (
                <SelectItem key={opt} value={opt}>
                  {t(`graphVizPreset_${opt}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Info strip */}
      <div className="text-xs text-slate-500 dark:text-slate-400 px-1">
        {t("graphVizInfo", {
          nodes: filtered.nodes.length,
          edges: filtered.edges.length,
          total: graphData.stats?.total_nodes ?? graphData.nodes.length,
        })}
      </div>

      {/* Canvas */}
      <div
        ref={containerRef}
        className="relative w-full aspect-video rounded-lg border border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-900 overflow-hidden"
      >
        <svg
          ref={svgRef}
          width={dims.w}
          height={dims.h}
          viewBox={`0 0 ${dims.w} ${dims.h}`}
          className="w-full h-full text-slate-400 dark:text-slate-600"
          role="group"
          aria-label={t("graphVizAriaLabel")}
        />

        {/* Legend — node type color map with counts */}
        {typeCounts.length > 0 && (
          <div className="absolute top-2 right-2 text-xs bg-white/80 dark:bg-black/60 backdrop-blur px-3 py-2 rounded-md border border-slate-200 dark:border-slate-700 space-y-1">
            {typeCounts.map(({ type, count, color }) => (
              <div key={type} className="flex items-center gap-2">
                <span
                  className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0"
                  style={{ backgroundColor: color }}
                />
                <span className="text-slate-700 dark:text-slate-300">
                  {type}
                </span>
                <span className="text-slate-400 dark:text-slate-500">
                  ({count})
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Hover tooltip */}
        {hovered && (
          <div className="absolute bottom-2 left-2 max-w-md text-xs text-slate-700 dark:text-slate-200 bg-white/90 dark:bg-black/80 backdrop-blur px-3 py-2 rounded-md border border-slate-200 dark:border-slate-700">
            <div className="font-semibold truncate">
              {hovered.summary || hovered.id}
            </div>
            <div className="text-slate-500">
              {t("graphVizTooltipDetail", {
                type: hovered.type,
                degree: hovered.degree,
                importance: hovered.importance,
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
