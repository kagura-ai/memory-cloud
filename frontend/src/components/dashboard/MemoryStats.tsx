'use client';

/**
 * Memory Stats Component
 *
 * Displays user's memory statistics using /api/v1/memory/stats
 * Issue #43: Real API implementation
 */

import { useEffect, useState } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Brain, Database, HardDrive, Clock, PieChart, XCircle } from 'lucide-react';
import { apiClient } from '@/lib/api';

interface MemoryStats {
  total_count: number;
  working_count: number;
  persistent_count: number;
  by_type: Record<string, number>;
  by_importance: Record<string, number>;
  recent_activity: number;
}

export function MemoryStats() {
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function fetchStats() {
      try {
        setIsLoading(true);
        const data = await apiClient.get<MemoryStats>('/api/v1/memory/stats');
        setStats(data);
        setError(null);
      } catch (err) {
        console.error('Failed to fetch memory stats:', err);
        setError(err instanceof Error ? err.message : 'Failed to load memory statistics');
      } finally {
        setIsLoading(false);
      }
    }

    fetchStats();
    // Refresh every 60 seconds
    const interval = setInterval(fetchStats, 60000);
    return () => clearInterval(interval);
  }, []);

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <Card key={i}>
              <CardContent className="p-6">
                <div className="h-24 animate-pulse rounded bg-gray-200" />
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (error || !stats) {
    return (
      <Alert variant="destructive">
        <XCircle className="h-4 w-4" />
        <AlertDescription>{error || 'Failed to load memory statistics'}</AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="space-y-6">
      {/* Main Stats Grid */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {/* Total Memories */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Total Memories</CardTitle>
            <Brain className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{stats.total_count.toLocaleString()}</div>
            <p className="text-xs text-muted-foreground">
              All memories stored
            </p>
          </CardContent>
        </Card>

        {/* Working Memory */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Working Memory</CardTitle>
            <Database className="h-4 w-4 text-orange-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-orange-600">{stats.working_count.toLocaleString()}</div>
            <p className="text-xs text-muted-foreground">
              Short-term storage
            </p>
          </CardContent>
        </Card>

        {/* Persistent Memory */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">Persistent Memory</CardTitle>
            <Database className="h-4 w-4 text-purple-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-purple-600">{stats.persistent_count.toLocaleString()}</div>
            <p className="text-xs text-muted-foreground">
              Long-term storage
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Type Breakdown */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <PieChart className="h-5 w-5" />
            Memory Breakdown by Type
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-2 md:grid-cols-3 lg:grid-cols-5">
            {Object.entries(stats.by_type).map(([type, count]) => (
              <div key={type} className="flex items-center justify-between p-3 bg-gray-50 rounded">
                <span className="text-sm font-medium capitalize">{type}</span>
                <span className="text-sm font-bold text-gray-700">{count}</span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Importance Breakdown */}
      <Card>
        <CardHeader>
          <CardTitle>Importance Distribution</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 md:grid-cols-3">
            <div className="p-4 bg-red-50 border border-red-200 rounded">
              <p className="text-sm text-red-700 font-medium">High Importance</p>
              <p className="text-2xl font-bold text-red-600">
                {stats.by_importance.high || 0}
              </p>
            </div>
            <div className="p-4 bg-yellow-50 border border-yellow-200 rounded">
              <p className="text-sm text-yellow-700 font-medium">Medium Importance</p>
              <p className="text-2xl font-bold text-yellow-600">
                {stats.by_importance.medium || 0}
              </p>
            </div>
            <div className="p-4 bg-gray-50 border border-gray-200 rounded">
              <p className="text-sm text-gray-700 font-medium">Low Importance</p>
              <p className="text-2xl font-bold text-gray-600">
                {stats.by_importance.low || 0}
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Recent Activity */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Clock className="h-5 w-5" />
            Recent Activity
          </CardTitle>
          <CardDescription>Memories added in the last 24 hours</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-bold text-green-600">{stats.recent_activity}</div>
          <p className="text-sm text-muted-foreground mt-2">
            {stats.recent_activity > 0 ? 'Active memory creation' : 'No recent activity'}
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
