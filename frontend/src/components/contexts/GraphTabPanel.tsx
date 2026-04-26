"use client";

/**
 * GraphTabPanel — Bounded neural graph visualization.
 *
 * Renders a d3-force based force-directed layout of the top-N nodes from the
 * context's neural memory graph. Gated by NEXT_PUBLIC_ENABLE_GRAPH_VIZ env var
 * at the page level (this component doesn't check the flag itself).
 *
 * Node click opens MemoryDetailDialog (deep-linked via `?memoryId=`,
 * shared with MemoriesTabPanel). Edge click renders a floating metadata
 * overlay anchored to the click point.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { graphApi } from "@/lib/api/graph";
import type { GraphData, GraphEdge, GraphNode } from "@/lib/types/graph";
import type { MemoryReference } from "@/lib/types/memory";
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
import { useToast } from "@/hooks/use-toast";
import { useMemoryDetailDialog } from "@/hooks/useMemoryDetailDialog";
import { useMemoryIdParam } from "@/hooks/useMemoryIdParam";
import { MemoryDetailDialog } from "@/components/memories/MemoryDetailDialog";
import { DeleteMemoryDialog } from "@/components/memories/DeleteMemoryDialog";
import { EditMemoryDialog } from "@/components/memories/EditMemoryDialog";
import { GraphEdgeOverlay } from "./GraphEdgeOverlay";

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

interface SelectedEdge {
  edge: GraphEdge;
  // Container-relative coordinates (clientX/clientY translated via
  // getBoundingClientRect on the canvas wrapper element).
  x: number;
  y: number;
}

export function GraphTabPanel({ contextId }: GraphTabPanelProps) {
  const t = useTranslations("contexts");
  const tMem = useTranslations("contextDetail.memoriesPanel");
  const { toast } = useToast();
  const [memoryIdParam, setMemoryIdParam] = useMemoryIdParam();

  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState<number>(50);
  const [strategy, setStrategy] = useState<FilterStrategy>("degree");
  const [preset, setPreset] = useState<PresetName>("default");
  const [hovered, setHovered] = useState<GraphNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<SelectedEdge | null>(null);
  const [dims, setDims] = useState<{ w: number; h: number }>({
    w: 600,
    h: 400,
  });

  const svgRef = useRef<SVGSVGElement | null>(null);
  // The canvas <div> is only mounted in the success branch (loading / error /
  // empty branches render different JSX), so a `useRef` initialized at hook
  // setup time would stay null when the panel first paints in a non-success
  // state. A callback ref re-runs on every mount/unmount of the wrapper,
  // letting the ResizeObserver effect attach as soon as the canvas appears
  // (and detach if we ever fall back to loading mid-life).
  const [containerEl, setContainerEl] = useState<HTMLDivElement | null>(null);

  const dialog = useMemoryDetailDialog({ memoryIdParam, setMemoryIdParam });

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
      setError(err instanceof Error ? err.message : t("graphVizLoadError"));
    } finally {
      setLoading(false);
    }
  }, [contextId, t]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // --- Responsive dims ---
  useEffect(() => {
    if (!containerEl) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        setDims({ w: Math.max(200, width), h: Math.max(200, height) });
      }
    });
    observer.observe(containerEl);
    return () => observer.disconnect();
  }, [containerEl]);

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

  // Title lookup for the edge overlay — falls back to the endpoint id when
  // the source/target node isn't in the filtered subset (rare but possible
  // when N is small and the edge endpoint was filtered out).
  const nodeById = useMemo(() => {
    const map = new Map<string, GraphNode>();
    for (const node of filtered.nodes) {
      map.set(node.id, node);
    }
    return map;
  }, [filtered.nodes]);

  // --- Type counts for legend ---
  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of filtered.nodes) {
      const key = node.type?.toLowerCase() ?? "unknown";
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
      .map(([type, count]) => ({
        type,
        count,
        color: TYPE_TO_CHART[type] ?? TYPE_TO_CHART.unknown,
      }));
  }, [filtered.nodes]);

  // Clear stale hover and edge selection when the filtered set changes (the
  // SVG is rebuilt and any cached edge reference becomes a dangling pointer).
  useEffect(() => {
    setHovered(null);
    setSelectedEdge(null);
  }, [filtered]);

  // Suppress hover so the tooltip doesn't flicker under the dialog when the
  // pointer stays over the circle after the click resolves.
  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      setHovered(null);
      setSelectedEdge(null);
      dialog.openDetail(node.id);
    },
    [dialog],
  );

  // Translate viewport coords into container-relative coords so the
  // absolute-positioned overlay sits at the click point. Closes the detail
  // dialog so dialog and overlay never compete for the same screen region.
  const handleEdgeClick = useCallback(
    (edge: GraphEdge, clientX: number, clientY: number) => {
      if (!containerEl) return;
      const rect = containerEl.getBoundingClientRect();
      dialog.handleDetailOpenChange(false);
      setSelectedEdge({
        edge,
        x: clientX - rect.left,
        y: clientY - rect.top,
      });
    },
    [dialog, containerEl],
  );

  // Stable identity so GraphEdgeOverlay's keydown/mousedown effect doesn't
  // re-attach listeners every panel render (e.g. on hover state changes).
  const handleOverlayClose = useCallback(() => setSelectedEdge(null), []);

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
    onNodeClick: handleNodeClick,
    onEdgeClick: handleEdgeClick,
  });

  // --- Dialog success handlers (panel-specific side effects) ---
  // The graph topology doesn't change on memory edit, so we don't refetch
  // graph data here — the node summary may be briefly stale until the user
  // changes filter/preset (which triggers fetch via the simulation rebuild).
  const handleEditSuccess = useCallback(
    (updated: MemoryReference) => {
      dialog.applyEditSuccess(updated);
      toast({ title: tMem("editSuccess") });
    },
    [dialog, toast, tMem],
  );

  // After delete the node disappears from the graph — refetch so the
  // visualization stays consistent with the underlying memory state.
  const handleDeleteSuccess = useCallback(() => {
    dialog.applyDeleteSuccess();
    toast({ title: tMem("deleteSuccess") });
    void fetchData();
  }, [dialog, toast, tMem, fetchData]);

  // The dialogs render alongside whatever the canvas branch picks below so
  // a deep-link URL hydrates the dialog the moment the panel mounts —
  // even while the graph data is still loading.
  const memoryDialogs = (
    <>
      <MemoryDetailDialog
        memory={dialog.hydrated}
        open={dialog.detailOpen}
        onOpenChange={dialog.handleDetailOpenChange}
        onEdit={dialog.hydrated ? dialog.handleDetailEdit : undefined}
        onDelete={dialog.handleDetailDelete}
        notFound={dialog.detailNotFound}
        outgoingLinks={dialog.linkedRefs.outgoing}
        outgoingHasMore={dialog.linkedRefs.outgoingHasMore}
        incomingLinks={dialog.linkedRefs.incoming}
        incomingHasMore={dialog.linkedRefs.incomingHasMore}
        onOpenLinkedMemory={dialog.openDetail}
      />
      {dialog.hydrated && (
        <DeleteMemoryDialog
          memory={dialog.hydrated}
          open={dialog.deleteOpen}
          onOpenChange={dialog.handleDeleteOpenChange}
          onSuccess={handleDeleteSuccess}
        />
      )}
      {dialog.hydrated && (
        <EditMemoryDialog
          memory={dialog.hydrated}
          open={dialog.editOpen}
          onOpenChange={dialog.handleEditOpenChange}
          onSuccess={handleEditSuccess}
        />
      )}
    </>
  );

  let canvas: React.ReactNode;
  if (loading) {
    canvas = <SpinnerLoading size="lg" message={t("graphVizLoading")} />;
  } else if (error) {
    canvas = <ErrorBanner error={error} />;
  } else if (!graphData || graphData.nodes.length === 0) {
    canvas = (
      <EmptyState
        icon={Brain}
        title={t("graphEmptyTitle")}
        description={t("graphEmpty")}
      />
    );
  } else {
    canvas = (
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
          ref={setContainerEl}
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

          {/* Hover tooltip — suppressed when an edge overlay is active so the
            two floating elements never compete for the same screen real estate. */}
          {hovered && !selectedEdge && (
            <div className="absolute bottom-2 left-2 max-w-md text-xs text-slate-700 dark:text-slate-200 bg-white/90 dark:bg-black/80 backdrop-blur px-3 py-2 rounded-md border border-slate-200 dark:border-slate-700">
              <div className="font-semibold truncate">
                {hovered.summary || hovered.id}
              </div>
              <div className="text-slate-500 dark:text-slate-400">
                {t("graphVizTooltipDetail", {
                  type: hovered.type,
                  degree: hovered.degree,
                  importance: hovered.importance,
                })}
              </div>
            </div>
          )}

          {selectedEdge && (
            <GraphEdgeOverlay
              edge={selectedEdge.edge}
              sourceTitle={
                nodeById.get(selectedEdge.edge.source)?.summary ||
                selectedEdge.edge.source
              }
              targetTitle={
                nodeById.get(selectedEdge.edge.target)?.summary ||
                selectedEdge.edge.target
              }
              x={selectedEdge.x}
              y={selectedEdge.y}
              containerWidth={dims.w}
              containerHeight={dims.h}
              onClose={handleOverlayClose}
            />
          )}
        </div>
      </div>
    );
  }

  return (
    <>
      {canvas}
      {memoryDialogs}
    </>
  );
}
