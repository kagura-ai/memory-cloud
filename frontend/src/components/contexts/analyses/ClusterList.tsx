"use client";

/**
 * ClusterList — sortable cluster overview with click-to-focus (Issue #497).
 *
 * Sorted by ``count DESC`` (largest cluster first). Clicking a row
 * toggles focus: same row again clears focus and returns to the
 * "All clusters" view. The state itself lives in
 * ``useFocusedClusterId`` and is URL-synced.
 */

import { useMemo } from "react";
import { useTranslations } from "next-intl";
import { EmptyState } from "@/components/ui/empty-state";
import { Layers, ArrowLeft } from "lucide-react";
import { getClusterColor } from "./clusterColors";
import { classifyQuality, formatConfidence } from "./analysisFormatters";
import type { AnalysisCluster } from "@/lib/api/analyses";

interface ClusterListProps {
  clusters: AnalysisCluster[];
  focusedClusterId: number | null;
  onToggleFocus: (clusterIndex: number) => void;
  onClearFocus: () => void;
}

export function ClusterList({
  clusters,
  focusedClusterId,
  onToggleFocus,
  onClearFocus,
}: ClusterListProps) {
  const t = useTranslations("analyses.clusters");

  const sorted = useMemo(
    () => [...clusters].sort((a, b) => b.count - a.count),
    [clusters],
  );

  if (sorted.length === 0) {
    return (
      <EmptyState
        compact
        icon={Layers}
        title={t("noClustersTitle")}
        description={t("noClustersDescription")}
      />
    );
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <header className="flex items-center justify-between border-b border-gray-100 px-5 py-4 dark:border-gray-800">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            {t("title")}
          </h3>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {t("subtitle", { count: sorted.length })}
          </p>
        </div>
        {focusedClusterId !== null && (
          <button
            type="button"
            onClick={onClearFocus}
            className="inline-flex items-center gap-1 rounded-md border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
          >
            <ArrowLeft className="h-3 w-3" />
            {t("allClusters")}
          </button>
        )}
      </header>
      <ul className="divide-y divide-gray-100 dark:divide-gray-800">
        {sorted.map((cluster) => {
          const isActive = cluster.cluster_index === focusedClusterId;
          const tier = classifyQuality(cluster.label_confidence);
          const tierClass =
            tier === "good"
              ? "text-emerald-700 dark:text-emerald-400"
              : tier === "fair"
                ? "text-amber-700 dark:text-amber-400"
                : "text-red-700 dark:text-red-400";
          return (
            <li key={cluster.cluster_index}>
              <button
                type="button"
                onClick={() => onToggleFocus(cluster.cluster_index)}
                aria-pressed={isActive}
                className={`flex w-full cursor-pointer items-start gap-3 px-5 py-3 text-left transition-colors hover:bg-gray-50 dark:hover:bg-gray-800/60 ${
                  isActive ? "bg-gray-50 dark:bg-gray-800/60" : ""
                }`}
              >
                <span
                  className="mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{
                    backgroundColor: getClusterColor(cluster.cluster_index),
                  }}
                  aria-hidden
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-semibold text-gray-900 dark:text-gray-100">
                      {cluster.label || t("outlierLabel")}
                    </span>
                    <span className="font-mono text-xs text-gray-400">
                      #{cluster.cluster_index}
                    </span>
                  </div>
                  {cluster.description && (
                    <p className="mt-0.5 line-clamp-1 text-xs text-gray-500 dark:text-gray-400">
                      {cluster.description}
                    </p>
                  )}
                </div>
                <div className="shrink-0 text-right">
                  <div className="text-base font-semibold text-gray-900 dark:text-gray-100">
                    {cluster.count}
                  </div>
                  <div className={`text-[10px] font-medium ${tierClass}`}>
                    {t("confidence", {
                      value: formatConfidence(cluster.label_confidence),
                    })}
                  </div>
                </div>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
