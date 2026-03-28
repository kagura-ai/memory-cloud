'use client';

/**
 * Memory Overview Component
 *
 * Displays memory and coding memory statistics from doctor API.
 * Issue #664: Web UI Redesign Phase 1
 */

import { useEffect, useState } from 'react';
import { SpinnerLoading } from '@/components/common/LoadingState';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  CheckCircle2,
  XCircle,
  AlertTriangle,
  Info,
  Database,
  Brain,
  Code2,
  FolderGit2,
  FileText,
  Network,
} from 'lucide-react';
import {
  getMemoryDoctor,
  getCodingDoctor,
  getSystemDoctor,
  type MemoryDoctorResponse,
  CodingDoctorResponse,
  type SystemDoctorResponse,
} from '@/lib/api/doctor';
import { MemoryStatsChart } from './MemoryStatsChart';

function getStatusIcon(status: string) {
  switch (status) {
    case 'ok':
      return <CheckCircle2 className="h-4 w-4 text-green-500" />;
    case 'warning':
      return <AlertTriangle className="h-4 w-4 text-yellow-500" />;
    case 'error':
      return <XCircle className="h-4 w-4 text-red-500" />;
    default:
      return <Info className="h-4 w-4 text-blue-500" />;
  }
}

function getStatusBadge(status: string) {
  switch (status) {
    case 'ok':
      return <Badge className="bg-green-500">Healthy</Badge>;
    case 'warning':
      return <Badge className="bg-yellow-500">Warning</Badge>;
    case 'error':
      return <Badge variant="destructive">Error</Badge>;
    default:
      return <Badge variant="secondary">Info</Badge>;
  }
}

/**
 * Parse Graph Memory statistics from System Doctor message
 * Example: "Connected (22 nodes, 21 edges, Backend: PostgreSQL)"
 */
function parseGraphStats(message: string | undefined): { nodes: number; edges: number } | null {
  if (!message) return null;

  const nodeMatch = message.match(/(\d+)\s+nodes/);
  const edgeMatch = message.match(/(\d+)\s+edges/);

  if (nodeMatch && edgeMatch) {
    return {
      nodes: parseInt(nodeMatch[1], 10),
      edges: parseInt(edgeMatch[1], 10),
    };
  }

  return null;
}

export function MemoryOverview() {
  const [memoryData, setMemoryData] = useState<MemoryDoctorResponse | null>(null);
  const [codingData, setCodingData] = useState<CodingDoctorResponse | null>(null);
  const [systemData, setSystemData] = useState<SystemDoctorResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchData() {
      try {
        setIsLoading(true);
        const [memory, coding, system] = await Promise.all([
          getMemoryDoctor(),
          getCodingDoctor(),
          getSystemDoctor(),
        ]);
        setMemoryData(memory);
        setCodingData(coding);
        setSystemData(system);
        setError(null);
      } catch (err) {
        console.error('Failed to fetch memory doctor:', err);
        setError(err instanceof Error ? err.message : 'Failed to load memory health');
      } finally {
        setIsLoading(false);
      }
    }

    fetchData();
  }, []);

  if (isLoading) {
    return (
      <div className="space-y-6">
        {/* Loading Message */}
        <div className="flex items-center justify-center rounded-2xl border-2 border-brand-green-200 bg-gradient-to-r from-brand-green-50 to-emerald-50 p-8">
          <SpinnerLoading size="lg" message="Loading memory metrics..." />
        </div>

        {/* Skeleton Cards */}
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <Card key={i} className="overflow-hidden">
              <CardContent className="p-6">
                <div className="h-32 animate-pulse rounded-lg bg-gradient-to-br from-brand-green-100 to-emerald-100" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (error || !memoryData || !codingData) {
    return (
      <Alert variant="destructive">
        <XCircle className="h-4 w-4" />
        <AlertDescription>{error || 'Failed to load memory statistics'}</AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="space-y-4">
      {/* Memory System Overview */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Brain className="h-5 w-5" />
            Memory System Health
          </CardTitle>
          <CardDescription>
            Overall status: {getStatusBadge(memoryData.status)}
          </CardDescription>
        </CardHeader>
      </Card>

      {/* Memory Metrics Grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {/* Database */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Database</CardTitle>
            <Database className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {memoryData.stats.database_exists ? (
                <>
                  {memoryData.stats.database_size_mb?.toFixed(1) || '0'}{' '}
                  <span className="text-sm">MB</span>
                </>
              ) : (
                'Not initialized'
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              {memoryData.stats.persistent_count.toLocaleString()} memories
            </p>
          </CardContent>
        </Card>

        {/* RAG Vectors */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">RAG Vectors</CardTitle>
            {getStatusIcon(memoryData.stats.rag_enabled ? 'ok' : 'warning')}
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {memoryData.stats.rag_count.toLocaleString()}
            </div>
            <p className="text-xs text-muted-foreground">
              {memoryData.stats.rag_enabled ? 'Enabled' : 'Disabled'}
            </p>
          </CardContent>
        </Card>

        {/* Reranking */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Reranking</CardTitle>
            {getStatusIcon(
              memoryData.stats.reranking_enabled
                ? 'ok'
                : memoryData.stats.reranking_model_installed
                  ? 'warning'
                  : 'info'
            )}
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {memoryData.stats.reranking_enabled ? 'Enabled' : 'Disabled'}
            </div>
            <p className="text-xs text-muted-foreground">
              Model: {memoryData.stats.reranking_model_installed ? 'Installed' : 'Not installed'}
            </p>
          </CardContent>
        </Card>

        {/* Coding Sessions */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Coding Sessions</CardTitle>
            <Code2 className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {codingData.stats.sessions_count.toLocaleString()}
            </div>
            <p className="text-xs text-muted-foreground">
              {codingData.stats.contexts_count} contexts
            </p>
          </CardContent>
        </Card>

        {/* Graph Memory */}
        {systemData?.graph_db && (() => {
          const graphStats = parseGraphStats(systemData.graph_db.message);
          return graphStats ? (
            <Card>
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">Graph Memory</CardTitle>
                <Network className="h-4 w-4 text-muted-foreground" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">
                  {graphStats.nodes.toLocaleString()}
                </div>
                <p className="text-xs text-muted-foreground">
                  {graphStats.edges.toLocaleString()} relationships
                </p>
              </CardContent>
            </Card>
          ) : null;
        })()}
      </div>


      {/* Memory & Coding Statistics Charts */}
      <MemoryStatsChart
        memoryData={memoryData.stats}
        codingData={codingData.stats}
      />

      {/* Recommendations */}
      {memoryData.recommendations.length > 0 && (
        <Alert>
          <Info className="h-4 w-4" />
          <AlertDescription>
            <div className="font-semibold mb-2">Recommendations:</div>
            <ul className="list-disc list-inside space-y-1">
              {memoryData.recommendations.map((rec, index) => (
                <li key={index} className="text-sm">
                  {rec}
                </li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
    </div>
  );
}
