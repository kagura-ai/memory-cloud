"use client";

/**
 * ScatterPlot — UMAP scatter for the analyses tab (Issue #497).
 *
 * SVG-based, per-cluster ``<g data-c="N">`` grouping so focus mode is
 * a CSS class toggle (``.scatter.focused .cluster-layer { opacity: .12 }``)
 * rather than an O(N) re-render. The render budget for v1 is bounded
 * by ``MemoryAnalysis.input_count`` ≤ 10k, which SVG handles as long as
 * every dot stays in a per-cluster group (browsers fold the opacity
 * toggle into a single GPU layer per cluster).
 *
 * Coordinate normalization: UMAP outputs arbitrary float ranges; we
 * normalize to ``[0, 1]`` against the run's bounding box, then scale
 * to the viewBox. ``computePositionBox`` + ``normalize01`` keep this
 * branch-free for the degenerate "everything at one coord" case.
 */

import { useMemo } from "react";
import { useTranslations } from "next-intl";
import { EmptyState } from "@/components/ui/empty-state";
import { Sparkles } from "lucide-react";
import { computePositionBox, normalize01 } from "./analysisFormatters";
import { getClusterColor } from "./clusterColors";
import type { AnalysisCluster, ScatterPosition } from "@/lib/api/analyses";

interface ScatterPlotProps {
  clusters: AnalysisCluster[];
  positions: ScatterPosition[];
  focusedClusterId: number | null;
  focusedCluster: AnalysisCluster | null;
  onClearFocus: () => void;
}

const VIEW_W = 800;
const VIEW_H = 520;
const PAD = 24;

export function ScatterPlot({
  clusters,
  positions,
  focusedClusterId,
  focusedCluster,
  onClearFocus,
}: ScatterPlotProps) {
  const t = useTranslations("analyses.scatter");

  const box = useMemo(() => computePositionBox(positions), [positions]);

  // Group positions by cluster_index so the SVG renders one <g> per
  // cluster — focus mode is then a single CSS class toggle on the
  // outer <svg>, no per-dot DOM diffing.
  const dotsByCluster = useMemo(() => {
    const map = new Map<number, ScatterPosition[]>();
    for (const p of positions) {
      const arr = map.get(p.cluster_index);
      if (arr) arr.push(p);
      else map.set(p.cluster_index, [p]);
    }
    return map;
  }, [positions]);

  if (clusters.length === 0 || positions.length === 0) {
    return (
      <EmptyState
        compact
        icon={Sparkles}
        title={t("title")}
        description={t("noPositions")}
      />
    );
  }

  const project = (x: number, y: number): [number, number] => {
    const nx = normalize01(x, box.minX, box.maxX);
    // Flip y so positive UMAP y goes up (SVG y grows downward).
    const ny = 1 - normalize01(y, box.minY, box.maxY);
    return [PAD + nx * (VIEW_W - PAD * 2), PAD + ny * (VIEW_H - PAD * 2)];
  };

  return (
    <div className="relative aspect-[1.55/1] w-full overflow-hidden rounded-lg border border-gray-100 bg-gray-50/40 dark:border-gray-800 dark:bg-gray-900/30">
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        className={`scatter h-full w-full ${
          focusedClusterId !== null ? "focused" : ""
        }`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={t("title")}
      >
        {clusters.map((cluster) => {
          const dots = dotsByCluster.get(cluster.cluster_index) ?? [];
          const color = getClusterColor(cluster.cluster_index);
          const [cx, cy] = project(
            cluster.centroid_2d[0],
            cluster.centroid_2d[1],
          );
          const isFocused = cluster.cluster_index === focusedClusterId;
          return (
            <g
              key={cluster.cluster_index}
              data-c={cluster.cluster_index}
              className={`cluster-layer ${isFocused ? "is-focused" : ""}`}
            >
              {dots.map((d) => {
                const [px, py] = project(d.x, d.y);
                return (
                  <circle
                    key={d.memory_id}
                    cx={px}
                    cy={py}
                    r={2.5}
                    fill={color}
                    opacity={0.85}
                  />
                );
              })}
              <circle
                cx={cx}
                cy={cy}
                r={5}
                fill={color}
                stroke="white"
                strokeWidth={2}
              />
              <text
                x={cx}
                y={cy - 12}
                fontSize={10}
                fontWeight={600}
                fill="currentColor"
                textAnchor="middle"
                className="pointer-events-none select-none fill-gray-900 dark:fill-gray-100"
              >
                {cluster.cluster_index}
              </text>
            </g>
          );
        })}
      </svg>

      {focusedCluster && (
        <div className="pointer-events-none absolute left-3 right-3 top-3 flex items-center justify-between">
          <div className="pointer-events-auto inline-flex items-center gap-2 rounded-md border border-gray-200 bg-white/95 px-3 py-1.5 text-xs shadow-sm backdrop-blur dark:border-gray-700 dark:bg-gray-900/95">
            <span
              className="h-2 w-2 rounded-full"
              style={{
                backgroundColor: getClusterColor(focusedCluster.cluster_index),
              }}
            />
            <span className="text-gray-500 dark:text-gray-400">
              {t("focusedOn")}
            </span>
            <span className="font-medium text-gray-900 dark:text-gray-100">
              {focusedCluster.label}
            </span>
            <span className="font-mono text-gray-400">
              · {focusedCluster.count}
            </span>
          </div>
          <button
            type="button"
            onClick={onClearFocus}
            className="pointer-events-auto inline-flex items-center gap-1 rounded-md border border-gray-200 bg-white/95 px-3 py-1.5 text-xs text-gray-700 shadow-sm backdrop-blur hover:bg-white dark:border-gray-700 dark:bg-gray-900/95 dark:text-gray-200 dark:hover:bg-gray-900"
          >
            ← {t("showAllClusters")}
          </button>
        </div>
      )}

      {/* Focus mode dimming via CSS — one rule, applied to the SVG root.
          Inline <style> is intentional: the rule is component-scoped and
          tightly coupled to the SVG class names above; promoting it to
          globals.css would orphan the rule from its single use site. */}
      <style jsx>{`
        :global(.scatter .cluster-layer) {
          transition:
            opacity 0.25s ease,
            transform 0.35s ease;
        }
        :global(.scatter.focused .cluster-layer) {
          opacity: 0.12;
        }
        :global(.scatter.focused .cluster-layer.is-focused) {
          opacity: 1;
        }
      `}</style>
    </div>
  );
}
