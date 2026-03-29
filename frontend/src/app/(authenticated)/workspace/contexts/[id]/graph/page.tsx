'use client';

/**
 * Neural Memory Graph View — Stats + Edge Table
 * Issue #31: Redesigned from React Flow to simple table view
 */

import { useEffect, useState, useCallback } from 'react';
import { useParams } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { graphApi } from '@/lib/api/graph';
import type { GraphData, GraphStatsResponse } from '@/lib/types/graph';
import { getMemoryTypeColor } from '@/lib/types/graph';
import { PageContainer } from '@/components/common/PageContainer';
import { PageHeader } from '@/components/common/PageHeader';
import { Brain, GitBranch, Activity, TrendingUp } from 'lucide-react';

export default function ContextGraphPage() {
  const params = useParams();
  const contextId = params.id as string;
  const t = useTranslations('contexts');

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
      setError(err instanceof Error ? err.message : 'Failed to load graph data');
    } finally {
      setLoading(false);
    }
  }, [contextId]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  if (loading) {
    return (
      <PageContainer>
        <div className="flex items-center justify-center h-64">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-brand-green-200 border-t-brand-green-600" />
        </div>
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title="Neural Memory Graph" description={contextId} />
        <div className="p-4 bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300 rounded-lg">
          {error}
        </div>
      </PageContainer>
    );
  }

  const s = stats?.stats;
  const nodes = graphData?.nodes || [];
  const edges = graphData?.edges || [];
  const topConnections = s?.top_connections || [];

  return (
    <PageContainer>
      <PageHeader title="Neural Memory Graph" description={`Context: ${contextId.slice(0, 8)}...`} />

      {/* Stats Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <StatCard icon={Brain} label={t('graphNodes', { default: 'Nodes' })} value={s?.total_nodes ?? 0} />
        <StatCard icon={GitBranch} label={t('graphEdges', { default: 'Edges' })} value={s?.total_edges ?? 0} />
        <StatCard icon={Activity} label={t('graphAvgWeight', { default: 'Avg Weight' })} value={s?.avg_edge_weight?.toFixed(4) ?? '0'} />
        <StatCard icon={TrendingUp} label={t('graphMaxWeight', { default: 'Max Weight' })} value={s?.max_edge_weight?.toFixed(4) ?? '0'} />
      </div>

      {/* Top Connected Nodes */}
      {topConnections.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
            {t('graphTopConnections', { default: 'Top Connected Memories' })}
          </h3>
          <div className="space-y-2">
            {topConnections.map((node, i) => (
              <div
                key={node.node_id}
                className="flex items-center gap-3 p-2 bg-gray-50 dark:bg-gray-800/50 rounded-lg"
              >
                <span className="text-xs font-mono text-gray-400 w-5">{i + 1}</span>
                <div className="flex-1 text-sm text-gray-900 dark:text-gray-100 truncate">
                  {node.summary || node.node_id.slice(0, 8)}
                </div>
                <span className="text-xs font-medium text-brand-green-600 dark:text-brand-green-400">
                  {node.degree} {t('graphConnections', { default: 'connections' })}
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
            {t('graphEdgeList', { default: 'Edges' })} ({edges.length})
          </h3>
          <div className="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                  <th className="px-4 py-2 text-left font-medium text-gray-600 dark:text-gray-300">Source</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-600 dark:text-gray-300">Target</th>
                  <th className="px-4 py-2 text-center font-medium text-gray-600 dark:text-gray-300">Weight</th>
                  <th className="px-4 py-2 text-center font-medium text-gray-600 dark:text-gray-300">Type</th>
                </tr>
              </thead>
              <tbody>
                {edges.map((edge, i) => {
                  const srcNode = nodes.find(n => n.id === edge.source);
                  const tgtNode = nodes.find(n => n.id === edge.target);
                  return (
                    <tr key={i} className="border-b border-gray-100 dark:border-gray-800">
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          {srcNode && (
                            <span
                              className="w-2 h-2 rounded-full flex-shrink-0"
                              style={{ backgroundColor: getMemoryTypeColor(srcNode.type) }}
                            />
                          )}
                          <span className="truncate max-w-[200px] text-gray-900 dark:text-gray-100">
                            {srcNode?.summary || edge.source.slice(0, 8)}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex items-center gap-2">
                          {tgtNode && (
                            <span
                              className="w-2 h-2 rounded-full flex-shrink-0"
                              style={{ backgroundColor: getMemoryTypeColor(tgtNode.type) }}
                            />
                          )}
                          <span className="truncate max-w-[200px] text-gray-900 dark:text-gray-100">
                            {tgtNode?.summary || edge.target.slice(0, 8)}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-2 text-center">
                        <div className="flex items-center justify-center gap-2">
                          <div className="w-16 bg-gray-200 dark:bg-gray-700 rounded-full h-1.5">
                            <div
                              className="bg-brand-green-500 h-1.5 rounded-full"
                              style={{ width: `${Math.min(edge.weight / 0.5 * 100, 100)}%` }}
                            />
                          </div>
                          <span className="text-xs font-mono text-gray-500 dark:text-gray-400 w-12">
                            {edge.weight.toFixed(4)}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-2 text-center">
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          {edge.type.replace('_', ' ')}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">
          <Brain className="h-12 w-12 mx-auto mb-3 opacity-30" />
          <p>{t('graphEmpty', { default: 'No neural edges yet. Use recall to build connections between memories.' })}</p>
        </div>
      )}
    </PageContainer>
  );
}

function StatCard({ icon: Icon, label, value }: { icon: React.ComponentType<{ className?: string }>; label: string; value: string | number }) {
  return (
    <div className="p-4 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg">
      <div className="flex items-center gap-2 mb-1">
        <Icon className="h-4 w-4 text-gray-400" />
        <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>
      </div>
      <p className="text-xl font-semibold text-gray-900 dark:text-gray-100">{value}</p>
    </div>
  );
}
