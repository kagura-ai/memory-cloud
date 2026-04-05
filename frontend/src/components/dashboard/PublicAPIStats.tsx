"use client";

/**
 * Public API Statistics Component
 *
 * Displays Resource Ingest and Public Search API usage statistics
 * Issue #265 - Public API usage stats for public contexts
 */

import { useTranslations } from "next-intl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  CartesianGrid,
} from "recharts";
import { Database, Search, TrendingUp } from "lucide-react";
import type { PublicAPIStatsResponse } from "@/lib/api/workspaces";
import { formatDate as formatDateUtil } from "@/lib/utils/datetime";
import { useAuth } from "@/contexts/AuthContext";

interface PublicAPIStatsProps {
  stats: PublicAPIStatsResponse;
  days: 7 | 30;
}

export function PublicAPIStats({ stats, days }: PublicAPIStatsProps) {
  const t = useTranslations("contextStats.publicAPI");
  const { user } = useAuth();

  const formatDate = (dateStr: string) => {
    try {
      return formatDateUtil(dateStr, user?.timezone);
    } catch {
      return dateStr;
    }
  };

  return (
    <div className="space-y-6">
      {/* Resource Ingest API Stats */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Database className="h-5 w-5 text-blue-500" />
            <div>
              <CardTitle>{t("resourceIngestAPI")}</CardTitle>
              <CardDescription>{t("resourceIngestDesc")}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Metrics Grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                {t("totalEvents")}
              </p>
              <p className="text-2xl font-bold text-green-600">
                {stats.resource_ingest?.total_events?.toLocaleString() ?? 0}
              </p>
            </div>
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                {t("lastNDays", { days })}
              </p>
              <p className="text-2xl font-bold">
                {stats.resource_ingest?.last_n_days?.toLocaleString() ?? 0}
              </p>
            </div>
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">{t("avgPerDay")}</p>
              <p className="text-2xl font-bold">
                {stats.resource_ingest?.avg_per_day?.toFixed(1) ?? "0.0"}
              </p>
            </div>
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                {t("activeTokens")}
              </p>
              <p className="text-2xl font-bold text-blue-600">
                {stats.resource_ingest?.active_tokens ?? 0}
              </p>
            </div>
          </div>

          {/* Timeline Chart */}
          {stats.resource_ingest?.timeline &&
            stats.resource_ingest.timeline.length > 0 && (
              <div className="space-y-2">
                <h4 className="font-semibold text-sm">{t("eventsTimeline")}</h4>
                <ResponsiveContainer width="100%" height={250}>
                  <LineChart
                    data={stats.resource_ingest.timeline}
                    aria-label={t("eventsTimeline")}
                  >
                    <CartesianGrid
                      strokeDasharray="3 3"
                      className="stroke-muted"
                    />
                    <XAxis
                      dataKey="date"
                      tickFormatter={formatDate}
                      className="text-xs"
                      aria-label="Date"
                    />
                    <YAxis className="text-xs" aria-label="Event count" />
                    <Tooltip
                      labelFormatter={(date) => formatDate(date)}
                      formatter={(value: number) => [value, t("events")]}
                      aria-label="Chart tooltip"
                    />
                    <Line
                      dataKey="count"
                      stroke="#10b981"
                      strokeWidth={2}
                      aria-label="Events per day"
                      dot={false}
                      name={t("events")}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
        </CardContent>
      </Card>

      {/* Public Search API Stats */}
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Search className="h-5 w-5 text-purple-500" />
            <div>
              <CardTitle>{t("publicSearchAPI")}</CardTitle>
              <CardDescription>{t("publicSearchDesc")}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Metrics Grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                {t("totalSearches")}
              </p>
              <p className="text-2xl font-bold text-purple-600">
                {stats.public_search?.total_searches?.toLocaleString() ?? 0}
              </p>
            </div>
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                {t("lastNDays", { days })}
              </p>
              <p className="text-2xl font-bold">
                {stats.public_search?.last_n_days?.toLocaleString() ?? 0}
              </p>
            </div>
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                {t("anonymousSearches")}
              </p>
              <p className="text-2xl font-bold text-orange-600">
                {stats.public_search?.anonymous?.toLocaleString() ?? 0}
              </p>
            </div>
            <div className="space-y-1">
              <p className="text-sm text-muted-foreground">
                {t("authenticatedSearches")}
              </p>
              <p className="text-2xl font-bold text-blue-600">
                {stats.public_search?.authenticated?.toLocaleString() ?? 0}
              </p>
            </div>
          </div>

          {/* Timeline Chart with 3 lines */}
          {stats.public_search?.timeline &&
            stats.public_search.timeline.length > 0 && (
              <div className="space-y-2">
                <h4 className="font-semibold text-sm">
                  {t("searchesTimeline")}
                </h4>
                <ResponsiveContainer width="100%" height={250}>
                  <LineChart
                    data={stats.public_search.timeline}
                    aria-label={t("searchesTimeline")}
                  >
                    <CartesianGrid
                      strokeDasharray="3 3"
                      className="stroke-muted"
                    />
                    <XAxis
                      dataKey="date"
                      tickFormatter={formatDate}
                      className="text-xs"
                      aria-label="Date"
                    />
                    <YAxis className="text-xs" aria-label="Search count" />
                    <Tooltip
                      labelFormatter={(date) => formatDate(date)}
                      formatter={(value: number, name: string) => {
                        const labels: Record<string, string> = {
                          total: t("total"),
                          anonymous: t("anonymous"),
                          authenticated: t("authenticated"),
                        };
                        return [value, labels[name] || name];
                      }}
                      aria-label="Chart tooltip"
                    />
                    <Line
                      dataKey="total"
                      stroke="#10b981"
                      strokeWidth={2}
                      dot={false}
                      name="total"
                      aria-label="Total searches per day"
                    />
                    <Line
                      dataKey="anonymous"
                      stroke="#f59e0b"
                      strokeWidth={2}
                      dot={false}
                      name="anonymous"
                      aria-label="Anonymous searches per day"
                    />
                    <Line
                      dataKey="authenticated"
                      stroke="#3b82f6"
                      strokeWidth={2}
                      dot={false}
                      name="authenticated"
                      aria-label="Authenticated searches per day"
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
        </CardContent>
      </Card>
    </div>
  );
}
