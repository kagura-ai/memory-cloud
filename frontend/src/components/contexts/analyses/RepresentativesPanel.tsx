"use client";

/**
 * RepresentativesPanel — top-k representative memories of focused cluster.
 *
 * Issue #497: the cluster row carries ``representative_memory_ids`` (UUID
 * array, capped at 5 by the labeler). This panel resolves them to
 * memory summaries via ``referenceMemory(uuid)`` so the cluster list
 * stays small (no embedded summaries) and importance / freshness
 * filtering can be added later without touching the analyses API.
 *
 * Defensive resolution: a representative_memory_id can become stale
 * if the underlying memory was soft-deleted post-run (model docstring
 * note on ``representative_memory_ids``). Failed lookups are silently
 * skipped — matching the backend's filter-out-stale semantics in
 * ``query_service.get_cluster``.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingState } from "@/components/common/LoadingState";
import { Users } from "lucide-react";
import { referenceMemory } from "@/lib/api/memory";
import { getClusterColor } from "./clusterColors";
import type { AnalysisCluster } from "@/lib/api/analyses";

interface RepresentativesPanelProps {
  cluster: AnalysisCluster | null;
  totalCount: number;
}

interface ResolvedRep {
  memory_id: string;
  summary: string;
  type: string;
  importance: number;
}

export function RepresentativesPanel({
  cluster,
  totalCount,
}: RepresentativesPanelProps) {
  const t = useTranslations("analyses.representatives");

  const ids = useMemo(
    () => (cluster ? cluster.representative_memory_ids.slice(0, 5) : []),
    [cluster],
  );
  // Stable key so the load effect doesn't re-fire on identical id lists.
  const idsKey = useMemo(() => ids.join(","), [ids]);

  const [resolved, setResolved] = useState<ResolvedRep[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (ids.length === 0) {
      setResolved([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);

    Promise.allSettled(ids.map((id) => referenceMemory(id))).then((results) => {
      if (cancelled) return;
      const next: ResolvedRep[] = [];
      for (const r of results) {
        if (r.status === "fulfilled") {
          const ref = r.value;
          next.push({
            memory_id: ref.memory_id,
            summary: ref.summary,
            type: ref.type,
            importance: ref.importance,
          });
        }
        // Rejected lookups are dropped — likely soft-deleted post-run.
      }
      setResolved(next);
      setLoading(false);
    });

    return () => {
      cancelled = true;
    };
    // idsKey captures content; depending on `ids` directly would
    // re-fire on identical-content array references.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey]);

  if (!cluster) {
    return (
      <EmptyState
        compact
        icon={Users}
        title={t("headerLabel")}
        description={t("emptyNoFocus")}
      />
    );
  }

  const accent = getClusterColor(cluster.cluster_index);

  return (
    <div className="rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <header className="flex items-center justify-between border-b border-gray-100 px-5 py-4 dark:border-gray-800">
        <div className="flex items-center gap-2">
          <span
            className="h-2 w-2 rounded-full"
            style={{ backgroundColor: accent }}
            aria-hidden
          />
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-700 dark:text-gray-300">
            {t("headerLabel")} ·{" "}
            <span className="text-sm font-medium normal-case tracking-normal text-gray-900 dark:text-gray-100">
              {cluster.label}
            </span>
          </h4>
        </div>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          {t("totalInCluster", { count: totalCount })}
        </span>
      </header>
      <div className="px-5 py-4">
        {loading ? (
          <LoadingState lines={3} />
        ) : resolved.length === 0 ? (
          <EmptyState
            compact
            icon={Users}
            title={t("empty")}
            description={t("emptyAfterLoad")}
          />
        ) : (
          <ul className="space-y-2">
            {resolved.map((rep) => (
              <li
                key={rep.memory_id}
                className="flex items-start gap-2.5 text-sm"
              >
                <span
                  className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ backgroundColor: accent }}
                  aria-hidden
                />
                <div className="min-w-0">
                  <div className="truncate text-gray-900 dark:text-gray-100">
                    {rep.summary}
                  </div>
                  <div className="mt-0.5 font-mono text-xs text-gray-400">
                    {t("metaLine", {
                      idShort: rep.memory_id.slice(0, 8),
                      type: rep.type,
                      importance: rep.importance.toFixed(2),
                    })}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
