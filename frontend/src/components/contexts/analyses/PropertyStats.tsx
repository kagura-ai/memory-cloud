"use client";

/**
 * PropertyStats — tag bar / type pie / importance histogram / time series.
 *
 * Driven by the ``property_stats`` JSONB blob persisted on each cluster
 * row. ``focusedClusterId === null`` → render the full-context aggregate
 * (sum across all clusters). Otherwise render the focused cluster's
 * stats directly.
 */

import { useMemo } from "react";
import { useTranslations } from "next-intl";
import { EmptyState } from "@/components/ui/empty-state";
import { BarChart3 } from "lucide-react";
import { getClusterColor } from "./clusterColors";
import {
  normalizePropertyStats,
  type NormalizedPropertyStats,
} from "./analysisFormatters";
import type { AnalysisCluster } from "@/lib/api/analyses";

interface PropertyStatsProps {
  clusters: AnalysisCluster[];
  focusedCluster: AnalysisCluster | null;
}

/** Sum two normalized stat blobs into one. Used for the "All clusters" view. */
function aggregateStats(
  blobs: NormalizedPropertyStats[],
): NormalizedPropertyStats {
  if (blobs.length === 0) {
    return {
      topTags: [],
      typeDistribution: [],
      importanceBuckets: [],
      timeSeries: [],
    };
  }
  const tagSums = new Map<string, number>();
  for (const blob of blobs) {
    for (const t of blob.topTags) {
      tagSums.set(t.tag, (tagSums.get(t.tag) ?? 0) + t.count);
    }
  }
  const topTags = [...tagSums.entries()]
    .sort(([, a], [, b]) => b - a)
    .slice(0, 8)
    .map(([tag, count]) => ({ tag, count }));

  // Aggregate types by summing the underlying COUNTS, not the per-
  // cluster ratios — ratio-of-ratios would weight a 5-memory cluster
  // and a 500-memory cluster equally, distorting the all-clusters
  // view. Ratios for the bar widths are computed at render time
  // from the summed counts (see render block below).
  const typeSums = new Map<string, number>();
  for (const blob of blobs) {
    for (const ty of blob.typeDistribution) {
      typeSums.set(ty.type, (typeSums.get(ty.type) ?? 0) + ty.count);
    }
  }
  const typeDistribution = [...typeSums.entries()]
    .sort(([, a], [, b]) => b - a)
    .slice(0, 6)
    .map(([type, count]) => ({ type, count }));

  const bucketLen = blobs[0].importanceBuckets.length;
  const importanceBuckets =
    bucketLen === 0
      ? []
      : Array.from({ length: bucketLen }, (_, i) =>
          blobs.reduce((acc, b) => acc + (b.importanceBuckets[i] ?? 0), 0),
        );

  const tsSums = new Map<string, number>();
  for (const blob of blobs) {
    for (const t of blob.timeSeries) {
      tsSums.set(t.bucket, (tsSums.get(t.bucket) ?? 0) + t.count);
    }
  }
  const timeSeries = [...tsSums.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([bucket, count]) => ({ bucket, count }));

  return { topTags, typeDistribution, importanceBuckets, timeSeries };
}

export function PropertyStats({
  clusters,
  focusedCluster,
}: PropertyStatsProps) {
  const t = useTranslations("analyses.propertyStats");

  const stats = useMemo(() => {
    if (focusedCluster) {
      return normalizePropertyStats(focusedCluster.property_stats);
    }
    return aggregateStats(
      clusters.map((c) => normalizePropertyStats(c.property_stats)),
    );
  }, [focusedCluster, clusters]);

  const accent = focusedCluster
    ? getClusterColor(focusedCluster.cluster_index)
    : "hsl(var(--chart-1))";

  const isEmpty =
    stats.topTags.length === 0 &&
    stats.typeDistribution.length === 0 &&
    stats.importanceBuckets.length === 0 &&
    stats.timeSeries.length === 0;

  if (isEmpty) {
    return (
      <EmptyState
        compact
        icon={BarChart3}
        title={t("title")}
        description={t("empty")}
      />
    );
  }

  const subtitle = focusedCluster
    ? t("subtitleCluster", {
        index: focusedCluster.cluster_index,
        label: focusedCluster.label,
      })
    : t("subtitleAll");

  const maxTagCount = Math.max(...stats.topTags.map((t) => t.count), 1);
  const maxBucket = Math.max(...stats.importanceBuckets, 1);
  const maxTs = Math.max(...stats.timeSeries.map((p) => p.count), 1);

  return (
    <div className="rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <header className="border-b border-gray-100 px-5 py-4 dark:border-gray-800">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
          {t("title")}
        </h3>
        <p className="inline-flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
          <span
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: accent }}
            aria-hidden
          />
          {subtitle}
        </p>
      </header>
      <div className="grid grid-cols-1 gap-x-6 gap-y-4 px-5 py-4 text-sm md:grid-cols-2">
        {stats.topTags.length > 0 && (
          <div>
            <div className="mb-2 text-xs text-gray-500 dark:text-gray-400">
              {t("topTags")}
            </div>
            <div className="space-y-1.5">
              {stats.topTags.map((row) => (
                <div key={row.tag} className="flex items-center gap-2">
                  <div className="w-24 truncate text-xs text-gray-700 dark:text-gray-300">
                    {row.tag}
                  </div>
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                    <div
                      className="h-full"
                      style={{
                        width: `${(row.count / maxTagCount) * 100}%`,
                        backgroundColor: accent,
                      }}
                    />
                  </div>
                  <div className="w-8 text-right font-mono text-xs text-gray-500 dark:text-gray-400">
                    {row.count}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {(stats.typeDistribution.length > 0 ||
          stats.importanceBuckets.length > 0) && (
          <div>
            {stats.typeDistribution.length > 0 &&
              (() => {
                // Compute ratios from the summed counts at the render
                // boundary so the per-cluster + all-clusters views
                // share the same denominator math (see aggregateStats
                // for why counts are carried instead of ratios).
                const typeTotal = stats.typeDistribution.reduce(
                  (acc, r) => acc + r.count,
                  0,
                );
                if (typeTotal === 0) return null;
                return (
                  <>
                    <div className="mb-2 text-xs text-gray-500 dark:text-gray-400">
                      {t("byType")}
                    </div>
                    <div className="space-y-1.5">
                      {stats.typeDistribution.map((row) => {
                        const ratio = row.count / typeTotal;
                        return (
                          <div
                            key={row.type}
                            className="flex items-center gap-2"
                          >
                            <div className="w-24 truncate text-xs text-gray-700 dark:text-gray-300">
                              {row.type}
                            </div>
                            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
                              <div
                                className="h-full bg-gray-700 dark:bg-gray-300"
                                style={{ width: `${ratio * 100}%` }}
                              />
                            </div>
                            <div className="w-8 text-right font-mono text-xs text-gray-500 dark:text-gray-400">
                              {Math.round(ratio * 100)}%
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </>
                );
              })()}

            {stats.importanceBuckets.length > 0 && (
              <>
                <div className="mb-2 mt-4 text-xs text-gray-500 dark:text-gray-400">
                  {t("importance")}
                </div>
                <div className="flex h-12 items-end gap-1.5">
                  {stats.importanceBuckets.map((count, i) => (
                    <div
                      key={i}
                      className="flex flex-1 flex-col items-center justify-end"
                    >
                      <div
                        className="w-full rounded-sm bg-emerald-400 dark:bg-emerald-500"
                        style={{
                          height: `${(count / maxBucket) * 100}%`,
                          minHeight: 2,
                        }}
                      />
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {stats.timeSeries.length > 0 && (
          <div className="md:col-span-2">
            <div className="mb-2 text-xs text-gray-500 dark:text-gray-400">
              {t("activity")}
            </div>
            <div className="flex h-10 items-end gap-0.5">
              {stats.timeSeries.map((p) => (
                <div
                  key={p.bucket}
                  className="flex-1 rounded-sm bg-gray-300 dark:bg-gray-600"
                  style={{
                    height: `${(p.count / maxTs) * 100}%`,
                    minHeight: 2,
                  }}
                  title={`${p.bucket}: ${p.count}`}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
