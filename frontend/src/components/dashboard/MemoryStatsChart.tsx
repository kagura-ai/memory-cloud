'use client';

/**
 * Memory Statistics Chart Component
 *
 * Visualizes memory and coding statistics with Recharts
 * Issue #672: UI Polish & Design Enhancement - Phase 3
 */

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { PieChart, Pie, Cell, ResponsiveContainer, Legend, Tooltip, BarChart, Bar, XAxis, YAxis, CartesianGrid } from 'recharts';
import { Brain, TrendingUp } from 'lucide-react';

interface MemoryStatsChartProps {
  memoryData: {
    persistent_count: number;
    rag_count: number;
    working_count?: number;
  };
  codingData: {
    sessions_count: number;
    contexts_count: number;
    active_sessions?: number;
  };
}

const COLORS = {
  persistent: '#3b82f6', // blue
  rag: '#10b981', // green
  working: '#f59e0b', // amber
  sessions: '#8b5cf6', // purple
  contexts: '#ec4899', // pink
};

export function MemoryStatsChart({ memoryData, codingData }: MemoryStatsChartProps) {
  // Memory distribution data
  const memoryDistribution = [
    { name: 'Persistent Memory', value: memoryData.persistent_count, color: COLORS.persistent },
    { name: 'RAG Vectors', value: memoryData.rag_count, color: COLORS.rag },
    { name: 'Working Memory', value: memoryData.working_count || 0, color: COLORS.working },
  ].filter(item => item.value > 0);

  // Coding statistics data
  const codingStats = [
    { name: 'Sessions', count: codingData.sessions_count, fill: COLORS.sessions },
    { name: 'Contexts', count: codingData.contexts_count, fill: COLORS.contexts },
    { name: 'Active', count: codingData.active_sessions || 0, fill: COLORS.rag },
  ].filter(item => item.count > 0);

  // Mock time series data for sessions (would come from API in production)
  const sessionsTrend = [
    { date: 'Mon', sessions: 12 },
    { date: 'Tue', sessions: 19 },
    { date: 'Wed', sessions: 15 },
    { date: 'Thu', sessions: 25 },
    { date: 'Fri', sessions: 22 },
    { date: 'Sat', sessions: 18 },
    { date: 'Sun', sessions: 20 },
  ];

  return (
    <div className="grid gap-4 md:grid-cols-2">
      {/* Memory Distribution Pie Chart */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Brain className="h-4 w-4" />
            Memory Distribution
          </CardTitle>
          <CardDescription>Breakdown of memory types and usage</CardDescription>
        </CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={250}>
            <PieChart>
              <Pie
                data={memoryDistribution}
                cx="50%"
                cy="50%"
                labelLine={false}
                label={({ name, percent }) => `${name}: ${percent ? (percent * 100).toFixed(0) : '0'}%`}
                outerRadius={80}
                fill="#8884d8"
                dataKey="value"
              >
                {memoryDistribution.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip />
            </PieChart>
          </ResponsiveContainer>
          <div className="mt-4 space-y-2">
            {memoryDistribution.map((item) => (
              <div key={item.name} className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  <div className="w-3 h-3 rounded-full" style={{ backgroundColor: item.color }} />
                  <span>{item.name}</span>
                </div>
                <span className="font-mono font-semibold">{item.value.toLocaleString()}</span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Coding Statistics Bar Chart */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <TrendingUp className="h-4 w-4" />
            Coding Statistics
          </CardTitle>
          <CardDescription>Overview of coding sessions and contexts</CardDescription>
        </CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={250}>
            <BarChart data={codingStats}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-800" />
              <XAxis dataKey="name" className="text-xs" />
              <YAxis className="text-xs" />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--background))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '6px',
                }}
              />
              <Bar dataKey="count" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>

      {/* Sessions Trend Chart - Full Width */}
      <Card className="md:col-span-2">
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <TrendingUp className="h-4 w-4" />
            Sessions Activity (Last 7 Days)
          </CardTitle>
          <CardDescription>Daily coding session activity trend</CardDescription>
        </CardHeader>
        <CardContent>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={sessionsTrend}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-800" />
              <XAxis dataKey="date" className="text-xs" />
              <YAxis className="text-xs" />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--background))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '6px',
                }}
              />
              <Bar dataKey="sessions" fill={COLORS.sessions} radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </CardContent>
      </Card>
    </div>
  );
}
