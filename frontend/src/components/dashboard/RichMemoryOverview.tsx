'use client';

/**
 * Rich Memory Overview Component
 *
 * Living Memory Dashboard with 4 sections:
 * 1. Memory Health Dashboard - Total, Working/Persistent, Storage, Trend
 * 2. Neural Memory Activity - Graph stats, Top connections
 * 3. Access Patterns - Most accessed, Type distribution
 * 4. Memory Evolution - Promotions, Decay, Background tasks
 *
 * Issue #46 Phase 5 - Rich Memory Overview
 * Issue #48: Manual refresh button (removed auto-refresh)
 * Issue #223: i18n support
 */

import { useEffect, useState, forwardRef, useImperativeHandle } from 'react';
import { useTranslations } from 'next-intl';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Brain, Database, HardDrive, Network, TrendingUp, PieChart, Activity, XCircle } from 'lucide-react';
import { apiClient } from '@/lib/api';
import { useMemoryContext } from '@/contexts/MemoryContextContext';
import {
  PieChart as RechartsPie,
  Pie,
  Cell,
  ResponsiveContainer,
  Tooltip,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
} from 'recharts';

interface MemoryStats {
  total_count: number;
  working_count: number;
  persistent_count: number;
  by_type: Record<string, number>;
}

interface GraphStats {
  user_id: string;
  stats: {
    total_nodes: number;
    total_edges: number;
    avg_edge_weight: number;
    max_edge_weight: number;
    min_edge_weight: number;
    density: number;
    top_connections: Array<{
      node_id: string;
      summary: string;
      type: string;
      degree: number;
      edge_count: number;
    }>;
  };
  last_updated: string;
}

interface AccessPatterns {
  most_accessed: Array<{
    memory_id: string;
    summary: string;
    type: string;
    access_count: number;
    last_used_at: string | null;
  }>;
  type_distribution: Record<string, number>;
  recent_access_count: number;
}

const COLORS = ['#10b981', '#3b82f6', '#8b5cf6', '#f59e0b', '#ef4444', '#06b6d4'];

export interface RichMemoryOverviewRef {
  refresh: () => Promise<void>;
}

export interface RichMemoryOverviewProps {
  contextId?: string;  // Optional: Override context (defaults to current context)
}

export const RichMemoryOverview = forwardRef<RichMemoryOverviewRef, RichMemoryOverviewProps>(
  ({ contextId: propContextId }, ref) => {
    const t = useTranslations('memoryOverview');

    const { contextId: currentContextId } = useMemoryContext();  // Issue #82: Track current context
    const contextId = propContextId || currentContextId;  // Use prop if provided, else current
  const [memoryStats, setMemoryStats] = useState<MemoryStats | null>(null);
  const [graphStats, setGraphStats] = useState<GraphStats | null>(null);
  const [accessPatterns, setAccessPatterns] = useState<AccessPatterns | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAllStats = async () => {
    try {
      setIsLoading(true);

      // Build context query parameter if contextId is provided
      const contextParam = contextId ? `?context_id=${contextId}` : '';

      // Fetch all stats in parallel
      const [memory, graph, access] = await Promise.all([
        apiClient.get<MemoryStats>(`/api/v1/memory/stats${contextParam}`),
        apiClient.get<GraphStats>(`/api/v1/graph/stats${contextParam}`),
        apiClient.get<AccessPatterns>(`/api/v1/memory/access-patterns?days=30${contextId ? `&context_id=${contextId}` : ''}`),
      ]);

      setMemoryStats(memory);
      setGraphStats(graph);
      setAccessPatterns(access);
      setError(null);
    } catch (err) {
      console.error('Failed to fetch overview stats:', err);
      setError(err instanceof Error ? err.message : t('failedToLoad'));
    } finally {
      setIsLoading(false);
    }
  };

  // Expose refresh function to parent via ref
  useImperativeHandle(ref, () => ({
    refresh: fetchAllStats,
  }));

  useEffect(() => {
    // Issue #82: Re-fetch when context changes
    if (contextId !== null) {
      fetchAllStats();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextId]);  // Only contextId to avoid infinite loop

  if (isLoading) {
    return (
      <div className="space-y-6">
        {[...Array(4)].map((_, i) => (
          <Card key={i}>
            <CardContent className="p-6">
              <div className="h-64 animate-pulse rounded bg-gray-200" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  if (error || !memoryStats || !graphStats || !accessPatterns) {
    return (
      <Alert variant="destructive">
        <XCircle className="h-4 w-4" />
        <AlertDescription>{error || t('failedToLoad')}</AlertDescription>
      </Alert>
    );
  }

  // Prepare data for charts
  const scopeData = [
    { name: t('working'), value: memoryStats.working_count, color: '#f59e0b' },
    { name: t('persistent'), value: memoryStats.persistent_count, color: '#8b5cf6' },
  ];

  const typeData = Object.entries(memoryStats.by_type).map(([type, count]) => ({
    name: type,
    value: count,
  }));

  const topConnectionsData = graphStats.stats.top_connections.slice(0, 10).map((conn) => ({
    name: conn.summary.substring(0, 40) + (conn.summary.length > 40 ? '...' : ''),
    edges: conn.edge_count,
    type: conn.type,
  }));

  return (
    <div className="space-y-6">
      {/* Section 1: Memory Health Dashboard */}
      <div>
        <h2 className="text-2xl font-bold mb-4 flex items-center gap-2">
          <Brain className="h-6 w-6 text-brand-green-600" />
          {t('memoryHealthDashboard')}
        </h2>

        <div className="grid gap-4 grid-cols-1 mb-6">
          {/* Total Memories - Hero Number */}
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium">{t('totalMemories')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-5xl font-bold text-brand-green-600">
                {memoryStats.total_count.toLocaleString()}
              </div>
              <p className="text-sm text-muted-foreground mt-2">
                {t('allMemoriesDesc')}
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Working vs Persistent - Donut Chart */}
        <Card>
          <CardHeader>
            <CardTitle>{t('workingVsPersistent')}</CardTitle>
            <CardDescription>{t('distributionByScope')}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <RechartsPie>
                  <Pie
                    data={scopeData}
                    cx="50%"
                    cy="50%"
                    innerRadius={60}
                    outerRadius={80}
                    paddingAngle={5}
                    dataKey="value"
                    label={(entry) => `${entry.name}: ${entry.value}`}
                  >
                    {scopeData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip />
                </RechartsPie>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Section 2: Neural Memory Activity */}
      <div>
        <h2 className="text-2xl font-bold mb-4 flex items-center gap-2">
          <Network className="h-6 w-6 text-purple-600" />
          {t('neuralActivity')}
        </h2>

        <div className="grid gap-4 grid-cols-2 md:grid-cols-4 mb-6">
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">{t('totalNodes')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-purple-600">
                {graphStats.stats.total_nodes}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm">{t('totalConnections')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-blue-600 dark:text-blue-400">
                {graphStats.stats.total_edges}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm">{t('graphDensity')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-brand-green-600">
                {(graphStats.stats.density * 100).toFixed(1)}%
              </div>
              <p className="text-xs text-muted-foreground mt-1">
                {graphStats.stats.total_edges} / {graphStats.stats.total_nodes * (graphStats.stats.total_nodes - 1)} {t('possible')}
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-sm">{t('avgConnectionStrength')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="text-3xl font-bold text-emerald-600">
                {graphStats.stats.avg_edge_weight.toFixed(3)}
              </div>
              <p className="text-xs text-muted-foreground mt-1">
                {t('max')}: {graphStats.stats.max_edge_weight.toFixed(3)}
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Top Connections - Bar Chart */}
        {topConnectionsData.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle>{t('topConnectedMemories')}</CardTitle>
              <CardDescription>{t('nodesWithMostRelationships')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={topConnectionsData}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis dataKey="name" />
                    <YAxis />
                    <Tooltip />
                    <Bar dataKey="edges" fill="#8b5cf6" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Section 3: Access Patterns */}
      <div>
        <h2 className="text-2xl font-bold mb-4 flex items-center gap-2">
          <Activity className="h-6 w-6 text-blue-600 dark:text-blue-400" />
          {t('accessPatterns')}
        </h2>

        <div className="grid gap-4 grid-cols-1 lg:grid-cols-2">
          {/* Most Accessed Memories */}
          <Card>
            <CardHeader>
              <CardTitle>{t('mostAccessedTop10')}</CardTitle>
              <CardDescription>{t('frequentlyRetrieved')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                {accessPatterns.most_accessed.length > 0 ? (
                  accessPatterns.most_accessed.map((mem) => (
                    <div
                      key={mem.memory_id}
                      className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 p-3 bg-gray-50 dark:bg-gray-800 rounded hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                    >
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <p className="text-sm font-medium text-gray-900 dark:text-gray-100 line-clamp-2 break-words">{mem.summary}</p>
                        <p className="text-xs text-gray-500 dark:text-gray-400">{mem.type}</p>
                      </div>
                      <div className="flex items-center gap-2 sm:ml-4 shrink-0">
                        <span className="text-sm font-bold text-blue-600 dark:text-blue-400">{mem.access_count}</span>
                        <span className="text-xs text-gray-400 dark:text-gray-500">{t('times')}</span>
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="text-sm text-gray-500 dark:text-gray-400">{t('noAccessData')}</p>
                )}
              </div>
            </CardContent>
          </Card>

          {/* Type Distribution - Pie Chart */}
          <Card>
            <CardHeader>
              <CardTitle>{t('memoryTypeDistribution')}</CardTitle>
              <CardDescription>{t('breakdownByType')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="h-64">
                {typeData.length > 0 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <RechartsPie>
                      <Pie
                        data={typeData}
                        cx="50%"
                        cy="50%"
                        labelLine={false}
                        label={(entry) => `${entry.name}: ${entry.value}`}
                        outerRadius={80}
                        dataKey="value"
                      >
                        {typeData.map((entry, index) => (
                          <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                        ))}
                      </Pie>
                      <Tooltip />
                    </RechartsPie>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <p className="text-sm text-gray-500 dark:text-gray-400">{t('noMemoryData')}</p>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>

    </div>
  );
});

RichMemoryOverview.displayName = 'RichMemoryOverview';
