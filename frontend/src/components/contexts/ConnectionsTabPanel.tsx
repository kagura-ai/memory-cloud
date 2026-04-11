/**
 * ConnectionsTabPanel
 *
 * Self-contained panel for the Connections tab in the consolidated context detail page.
 * Contains neural memory graph stats and edge table.
 * Extracted from contexts/[id]/graph/page.tsx (#232).
 */

"use client";

import { useEffect, useState, useCallback, useMemo } from "react";
import { useTranslations } from "next-intl";
import { graphApi } from "@/lib/api/graph";
import type { ApiError } from "@/lib/api/base";
import type { GraphData, GraphStatsResponse } from "@/lib/types/graph";
import { getMemoryTypeColor } from "@/lib/types/graph";
import { Brain, GitBranch, Activity, TrendingUp } from "lucide-react";
import { KpiCard } from "@/components/ui/kpi-card";
import { TableLoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

interface ConnectionsTabPanelProps {
  contextId: string;
}

export function ConnectionsTabPanel({ contextId }: ConnectionsTabPanelProps) {
  const t = useTranslations("contexts");

  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [stats, setStats] = useState<GraphStatsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    if (!contextId) return;
    setLoading(true);
    setError(null);

    try {
      const [dataResult, statsResult] = await Promise.all([
        graphApi.getGraphData(contextId, { limit_nodes: 200 }),
        graphApi.getGraphStats(contextId),
      ]);
      setGraphData(dataResult);
      setStats(statsResult);
    } catch (err) {
      // apiClient throws plain-object ApiError (see frontend/src/lib/api/base.ts),
      // so `err instanceof Error` is false — use a shape check via Partial<ApiError>.
      // This also handles real Error instances (which have .message via prototype).
      const apiErr = err as Partial<ApiError> | null;
      setError(apiErr?.message ?? "Failed to load graph data");
    } finally {
      setLoading(false);
    }
  }, [contextId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const s = stats?.stats;
  const nodes = graphData?.nodes || [];
  const edges = graphData?.edges || [];
  const topConnections = s?.top_connections || [];
  const nodeById = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);

  if (loading) {
    return <TableLoadingState rows={5} />;
  }

  if (error) {
    return <ErrorBanner error={error} />;
  }

  return (
    <div className="space-y-6">
      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KpiCard
          icon={Brain}
          label={t("graphNodes", { default: "Nodes" })}
          value={s?.total_nodes ?? 0}
        />
        <KpiCard
          icon={GitBranch}
          label={t("graphEdges", { default: "Edges" })}
          value={s?.total_edges ?? 0}
        />
        <KpiCard
          icon={Activity}
          label={t("graphAvgWeight", { default: "Avg Weight" })}
          value={s?.avg_edge_weight?.toFixed(4) ?? "0"}
        />
        <KpiCard
          icon={TrendingUp}
          label={t("graphMaxWeight", { default: "Max Weight" })}
          value={s?.max_edge_weight?.toFixed(4) ?? "0"}
        />
      </div>

      {/* Top Connected Nodes */}
      {topConnections.length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
            {t("graphTopConnections", { default: "Top Connected Memories" })}
          </h3>
          <div className="space-y-2">
            {topConnections.map((node, i) => (
              <div
                key={node.node_id}
                className="flex items-center gap-3 p-2 bg-gray-50 dark:bg-gray-800/50 rounded-lg"
              >
                <span className="text-xs font-mono text-gray-400 w-5">
                  {i + 1}
                </span>
                <div className="flex-1 text-sm text-gray-900 dark:text-gray-100 truncate">
                  {node.summary || node.node_id.slice(0, 8)}
                </div>
                <span className="text-xs font-medium text-brand-green-600 dark:text-brand-green-400">
                  {node.degree}{" "}
                  {t("graphConnections", { default: "connections" })}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Edge Table */}
      {edges.length > 0 ? (
        <div>
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
            {t("graphEdgeList", { default: "Edges" })} ({edges.length})
          </h3>
          <div className="rounded-lg border border-slate-200 dark:border-slate-800">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>
                    {t("graphSource", { default: "Source" })}
                  </TableHead>
                  <TableHead>
                    {t("graphTarget", { default: "Target" })}
                  </TableHead>
                  <TableHead className="text-center">
                    {t("graphWeight", { default: "Weight" })}
                  </TableHead>
                  <TableHead className="text-center">
                    {t("graphType", { default: "Type" })}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {edges.map((edge, i) => {
                  const srcNode = nodeById.get(edge.source);
                  const tgtNode = nodeById.get(edge.target);
                  return (
                    <TableRow key={i}>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          {srcNode && (
                            <span
                              className="w-2 h-2 rounded-full flex-shrink-0"
                              style={{
                                backgroundColor: getMemoryTypeColor(
                                  srcNode.type,
                                ),
                              }}
                            />
                          )}
                          <span className="truncate max-w-[200px] text-gray-900 dark:text-gray-100">
                            {srcNode?.summary || edge.source.slice(0, 8)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-2">
                          {tgtNode && (
                            <span
                              className="w-2 h-2 rounded-full flex-shrink-0"
                              style={{
                                backgroundColor: getMemoryTypeColor(
                                  tgtNode.type,
                                ),
                              }}
                            />
                          )}
                          <span className="truncate max-w-[200px] text-gray-900 dark:text-gray-100">
                            {tgtNode?.summary || edge.target.slice(0, 8)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-center">
                        <div className="flex items-center justify-center gap-2">
                          <div className="w-16 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                            <div
                              className="bg-brand-green-500 h-1.5 rounded-full"
                              style={{
                                width: `${Math.min((edge.weight / (s?.max_edge_weight || 0.5)) * 100, 100)}%`,
                              }}
                            />
                          </div>
                          <span className="text-xs font-mono text-gray-500 dark:text-gray-400 w-12">
                            {edge.weight.toFixed(4)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-center">
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          {edge.type.replace("_", " ")}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </div>
      ) : (
        <EmptyState
          icon={Brain}
          title={t("graphEmptyTitle", { default: "No neural edges yet" })}
          description={t("graphEmpty", {
            default: "Use recall to build connections between memories.",
          })}
        />
      )}
    </div>
  );
}
