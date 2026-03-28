'use client';

/**
 * System Telemetry Component
 *
 * Displays system health and telemetry using /api/v1/telemetry
 * Issue #43: Real API implementation
 */

import { useEffect, useState } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { CheckCircle2, XCircle, Database, HardDrive, Network } from 'lucide-react';
import { apiClient } from '@/lib/api';

interface ServiceStatus {
  status: string;
  version?: string;
  details?: any;
}

interface TelemetryData {
  services: {
    postgres: ServiceStatus;
    qdrant: ServiceStatus;
    redis: ServiceStatus;
  };
  memory_stats: {
    total: number;
    working: number;
    persistent: number;
  };
  neural_memory: {
    nodes: number;
    edges: number;
  };
  uptime_seconds: number;
  version: string;
}

export function SystemTelemetry() {
  const [telemetry, setTelemetry] = useState<TelemetryData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchTelemetry() {
      try {
        setIsLoading(true);
        const data = await apiClient.get<TelemetryData>('/api/v1/system/telemetry');
        setTelemetry(data);
        setError(null);
      } catch (err) {
        console.error('Failed to fetch telemetry:', err);
        setError(err instanceof Error ? err.message : 'Failed to load system telemetry');
      } finally {
        setIsLoading(false);
      }
    }

    fetchTelemetry();
    // Refresh every 30 seconds
    const interval = setInterval(fetchTelemetry, 30000);
    return () => clearInterval(interval);
  }, []);

  const formatUptime = (seconds: number) => {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return `${days}d ${hours}h ${minutes}m`;
  };

  const getStatusColor = (status: string) => {
    return status === 'ok' ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200';
  };

  if (isLoading) {
    return (
      <div className="grid gap-4 md:grid-cols-3">
        {[...Array(3)].map((_, i) => (
          <Card key={i}>
            <CardContent className="p-6">
              <div className="h-24 animate-pulse rounded bg-gray-200" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  if (error || !telemetry) {
    return (
      <Alert variant="destructive">
        <XCircle className="h-4 w-4" />
        <AlertDescription>{error || 'Failed to load system telemetry'}</AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="space-y-6">
      {/* Services Status */}
      <div className="grid gap-4 md:grid-cols-3">
        {/* PostgreSQL */}
        <Card className={getStatusColor(telemetry.services.postgres.status)}>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">PostgreSQL</CardTitle>
            {telemetry.services.postgres.status === 'ok' ? (
              <CheckCircle2 className="h-4 w-4 text-green-600" />
            ) : (
              <XCircle className="h-4 w-4 text-red-600" />
            )}
          </CardHeader>
          <CardContent>
            <div className="text-lg font-semibold">
              {telemetry.services.postgres.status === 'ok' ? 'Connected' : 'Error'}
            </div>
            <p className="text-xs text-muted-foreground">
              {telemetry.services.postgres.version || 'Database'}
            </p>
          </CardContent>
        </Card>

        {/* Qdrant */}
        <Card className={getStatusColor(telemetry.services.qdrant.status)}>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Qdrant</CardTitle>
            {telemetry.services.qdrant.status === 'ok' ? (
              <CheckCircle2 className="h-4 w-4 text-green-600" />
            ) : (
              <XCircle className="h-4 w-4 text-red-600" />
            )}
          </CardHeader>
          <CardContent>
            <div className="text-lg font-semibold">
              {telemetry.services.qdrant.status === 'ok' ? 'Connected' : 'Error'}
            </div>
            <p className="text-xs text-muted-foreground">
              {telemetry.services.qdrant.details?.collections || 0} collections
            </p>
          </CardContent>
        </Card>

        {/* Redis */}
        <Card className={getStatusColor(telemetry.services.redis.status)}>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Redis</CardTitle>
            {telemetry.services.redis.status === 'ok' ? (
              <CheckCircle2 className="h-4 w-4 text-green-600" />
            ) : (
              <XCircle className="h-4 w-4 text-red-600" />
            )}
          </CardHeader>
          <CardContent>
            <div className="text-lg font-semibold">
              {telemetry.services.redis.status === 'ok' ? 'Connected' : 'Error'}
            </div>
            <p className="text-xs text-muted-foreground">
              {telemetry.services.redis.details?.memory_mb?.toFixed(2) || 0} MB
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Memory Stats */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="h-5 w-5" />
            Your Memory Usage
          </CardTitle>
          <CardDescription>Current memory storage statistics</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-3">
            <div className="p-4 bg-blue-50 rounded">
              <p className="text-sm text-blue-700 font-medium">Total Memories</p>
              <p className="text-2xl font-bold text-blue-600">
                {telemetry.memory_stats.total.toLocaleString()}
              </p>
            </div>
            <div className="p-4 bg-orange-50 rounded">
              <p className="text-sm text-orange-700 font-medium">Working Memory</p>
              <p className="text-2xl font-bold text-orange-600">
                {telemetry.memory_stats.working.toLocaleString()}
              </p>
            </div>
            <div className="p-4 bg-purple-50 rounded">
              <p className="text-sm text-purple-700 font-medium">Persistent Memory</p>
              <p className="text-2xl font-bold text-purple-600">
                {telemetry.memory_stats.persistent.toLocaleString()}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Neural Memory */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Network className="h-5 w-5" />
            Neural Memory Graph
          </CardTitle>
          <CardDescription>Learned associations and knowledge graph</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="p-4 bg-green-50 rounded">
              <p className="text-sm text-green-700 font-medium">Graph Nodes</p>
              <p className="text-2xl font-bold text-green-600">
                {telemetry.neural_memory.nodes.toLocaleString()}
              </p>
              <p className="text-xs text-green-600 mt-1">Memory concepts</p>
            </div>
            <div className="p-4 bg-teal-50 rounded">
              <p className="text-sm text-teal-700 font-medium">Graph Edges</p>
              <p className="text-2xl font-bold text-teal-600">
                {telemetry.neural_memory.edges.toLocaleString()}
              </p>
              <p className="text-xs text-teal-600 mt-1">Learned associations</p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* System Info */}
      <Card>
        <CardHeader>
          <CardTitle>System Information</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <p className="text-sm text-gray-500">Version</p>
              <p className="text-lg font-semibold">{telemetry.version}</p>
            </div>
            <div>
              <p className="text-sm text-gray-500">Uptime</p>
              <p className="text-lg font-semibold">{formatUptime(telemetry.uptime_seconds)}</p>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
