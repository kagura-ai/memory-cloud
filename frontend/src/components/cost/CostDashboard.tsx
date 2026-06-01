"use client";

/**
 * Shared cost-aggregation dashboard UI.
 *
 * Issue #473: rendered by both ``/admin/cost`` (cross-workspace) and
 * ``/workspace/cost`` (single-workspace, owner/admin scoped). The two
 * pages differ only in the fetcher and a couple of optional tweaks
 * (workspace column visibility, page title/description) — everything
 * else (date range UI, period toggle, chart, table, sticky-NULL
 * rendering) is identical and lives here.
 *
 * Sticky-NULL contract (mirrors the backend service shipped in #472):
 * a row whose ``cost_usd`` is ``null`` means "cost unknown" — at least
 * one contributing usage row had no resolved pricing. Render as "—"
 * (table) or as a chart gap (``connectNulls={false}``), NOT as
 * ``$0.00``, so operators can see the difference between "no spend"
 * and "spend we couldn't price".
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import {
  LoadingState,
  TableLoadingState,
  InlineSpinner,
} from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { RefreshCw, BarChart3 } from "lucide-react";
import { formatLocalDate } from "@/lib/utils/datetime";
import {
  COST_AGGREGATION_PERIODS,
  type CostAggregationPeriod,
  type CostAggregationResponse,
  type CostAggregationRow,
} from "@/lib/api";

const DEFAULT_LOOKBACK_DAYS = 30;
// MUST stay in sync with the backend cap (#528):
// backend/src/services/cost_aggregation_service.py `MAX_LOOKBACK_DAYS`.
// A one-sided bump makes the UI accept a window the API then rejects with a
// 400. Exported + pinned by a test so a change here trips CI; bump both sides.
export const MAX_LOOKBACK_DAYS = 365;

function defaultDateRange(): { from: string; to: string } {
  const today = new Date();
  const past = new Date(today);
  past.setDate(today.getDate() - (DEFAULT_LOOKBACK_DAYS - 1));
  return { from: formatLocalDate(past), to: formatLocalDate(today) };
}

/**
 * Render a nullable cost as USD or em-dash. Null means "cost unknown"
 * — distinguish from a genuine $0 in the UI.
 *
 * Exported for unit testing; not re-exported from the package barrel.
 */
export function formatCost(value: number | null): string {
  if (value === null) return "—";
  return `$${value.toFixed(4)}`;
}

export interface ChartPoint {
  date: string;
  cost_usd: number | null;
  cost_usd_byok: number | null;
}

/**
 * Collapse aggregation rows by ``period_start`` for the time-series
 * chart. Sticky-NULL: any null contribution to a bucket makes the
 * whole bucket null, so the chart renders a gap (with
 * ``connectNulls={false}``) for "cost unknown" periods rather than
 * understating spend with a misleading $0 dip. Output is sorted
 * lexicographically by date (which works for ISO YYYY-MM-DD).
 *
 * Exported for unit testing.
 */
export function buildChartData(rows: CostAggregationRow[]): ChartPoint[] {
  const buckets = new Map<
    string,
    { cost_usd: number | null; cost_usd_byok: number | null }
  >();
  for (const row of rows) {
    const existing = buckets.get(row.period_start) ?? {
      cost_usd: 0,
      cost_usd_byok: 0,
    };
    const next = {
      cost_usd:
        existing.cost_usd === null || row.cost_usd === null
          ? null
          : existing.cost_usd + row.cost_usd,
      cost_usd_byok:
        existing.cost_usd_byok === null || row.cost_usd_byok === null
          ? null
          : existing.cost_usd_byok + row.cost_usd_byok,
    };
    buckets.set(row.period_start, next);
  }
  return [...buckets.entries()]
    .map(([date, totals]) => ({ date, ...totals }))
    .sort((a, b) => a.date.localeCompare(b.date));
}

/** Filter shape passed to the fetcher. Same fields both endpoints accept. */
export interface CostDashboardFetchParams {
  period: CostAggregationPeriod;
  from: string;
  to: string;
}

export interface CostDashboardProps {
  /** Page title (translated by the caller). */
  title: string;
  /** Page description (translated by the caller). */
  description: string;
  /**
   * Fetcher injected by the page wrapper. The wrapper closes over the
   * right endpoint (admin cross-workspace vs workspace-scoped) and any
   * path params so the dashboard itself stays endpoint-agnostic.
   */
  fetchData: (
    params: CostDashboardFetchParams,
  ) => Promise<CostAggregationResponse>;
  /**
   * Whether to render the per-row workspace_id column. The
   * admin (cross-workspace) page wants it; the workspace-scoped page
   * doesn't (every row is the same workspace).
   */
  showWorkspaceColumn?: boolean;
  /**
   * Disable initial fetch — useful when the parent is still resolving
   * a prerequisite (e.g. ``currentWorkspaceId`` not yet available).
   * The dashboard renders an empty state until ``true``.
   */
  ready?: boolean;
}

export function CostDashboard({
  title,
  description,
  fetchData,
  showWorkspaceColumn = false,
  ready = true,
}: CostDashboardProps) {
  const t = useTranslations("admin.cost");

  const [{ from, to }, setRange] = useState(defaultDateRange);
  const [period, setPeriod] = useState<CostAggregationPeriod>("day");
  const [rows, setRows] = useState<CostAggregationRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  // Client-side range validation. The server validates `period` and
  // `from <= to` (#472), but does NOT enforce a maximum window today —
  // so MAX_LOOKBACK_DAYS is a UI-only soft cap to keep accidental huge
  // queries from hitting the backend. A non-UI caller (curl / SDK)
  // can still request arbitrarily large ranges. Backend enforcement
  // is tracked as a follow-up to #472.
  const rangeError = useMemo<string | null>(() => {
    if (!from || !to) return null;
    if (from > to) return t("validation.invertedRange");
    // Use UTC anchors for the day-window math. Local-tz construction
    // (`new Date(\`${from}T00:00:00\`)`) drifts ±1 across DST transitions
    // because a local "day" is 23/25 hours those days, which can flip
    // a 365-day window to 364 or 366 and spuriously trigger / suppress
    // the windowTooWide error at the boundary. Date.UTC always
    // returns 86,400,000 ms per day.
    const [fy, fm, fd] = from.split("-").map(Number);
    const [ty, tm, td] = to.split("-").map(Number);
    const days =
      Math.round(
        (Date.UTC(ty, tm - 1, td) - Date.UTC(fy, fm - 1, fd)) / 86400000,
      ) + 1;
    if (days > MAX_LOOKBACK_DAYS) {
      return t("validation.windowTooWide", { max: MAX_LOOKBACK_DAYS });
    }
    return null;
  }, [from, to, t]);

  const loadCost = useCallback(async () => {
    if (!ready) {
      // Clear stale state from a prior fetch — otherwise a workspace
      // switch (which transiently flips ready→false) would leave the
      // previous workspace's error banner AND chart/table data visible
      // while the new workspace_id resolves.
      setError(null);
      setRows([]);
      setLoading(false);
      return;
    }
    // Short-circuit on client-side validation failure: firing the
    // fetch with an invalid range (inverted, > MAX_LOOKBACK_DAYS) just
    // wastes a round-trip and risks flashing a backend 400 error
    // banner over the more specific rangeError banner that's already
    // showing in the filter section. Also reset error + rows so the UI
    // doesn't keep showing chart/table data from the previous (valid)
    // range while the user is mid-correction.
    if (rangeError !== null) {
      setError(null);
      setRows([]);
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const response = await fetchData({ period, from, to });
      setRows(response.rows);
    } catch (err) {
      setError(
        err instanceof Error
          ? err
          : new Error(typeof err === "string" ? err : t("errors.unknown")),
      );
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [from, to, period, ready, rangeError, fetchData, t]);

  useEffect(() => {
    loadCost();
  }, [loadCost]);

  const chartData = useMemo(() => buildChartData(rows), [rows]);

  return (
    <PageContainer>
      <PageHeader
        title={title}
        description={description}
        actions={
          <Button
            onClick={loadCost}
            variant="outline"
            disabled={loading || rangeError !== null}
          >
            {loading ? (
              <InlineSpinner size="sm" className="mr-2" />
            ) : (
              <RefreshCw className="h-4 w-4 mr-2" />
            )}
            {t("actions.refresh")}
          </Button>
        }
      />

      <Section title={t("filter.title")}>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-4">
          <div>
            <Label htmlFor="cost-from">{t("filter.from")}</Label>
            <Input
              id="cost-from"
              type="date"
              value={from}
              onChange={(e) =>
                setRange((r) => ({ ...r, from: e.target.value }))
              }
              max={to}
            />
          </div>
          <div>
            <Label htmlFor="cost-to">{t("filter.to")}</Label>
            <Input
              id="cost-to"
              type="date"
              value={to}
              onChange={(e) => setRange((r) => ({ ...r, to: e.target.value }))}
              min={from}
              max={formatLocalDate(new Date())}
            />
          </div>
          <div className="md:col-span-2">
            <Label>{t("filter.period")}</Label>
            <Tabs
              value={period}
              onValueChange={(v) => setPeriod(v as CostAggregationPeriod)}
              className="mt-1.5"
            >
              <TabsList>
                {COST_AGGREGATION_PERIODS.map((p) => (
                  <TabsTrigger key={p} value={p}>
                    {t(`period.${p}`)}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
          </div>
        </div>
        {rangeError && <ErrorBanner error={rangeError} />}
      </Section>

      <Section title={t("chart.title")}>
        {/* Range-error placeholder check ordered first so the section
            doesn't fall through to "No cost data" while the user is
            mid-correction — the rangeError banner in the filter
            section already explains WHAT is wrong; here we just hint
            that the chart will populate once they fix it. */}
        {rangeError !== null ? (
          <p className="text-sm text-gray-500 dark:text-gray-400 p-6">
            {t("rangeErrorPlaceholder")}
          </p>
        ) : loading ? (
          <div className="p-6">
            <LoadingState lines={6} />
          </div>
        ) : error ? (
          <ErrorBanner error={error.message} />
        ) : chartData.length === 0 ? (
          <EmptyState
            icon={BarChart3}
            title={t("emptyState.title")}
            description={t("emptyState.description")}
          />
        ) : (
          <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
            <ResponsiveContainer width="100%" height={320}>
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" />
                <YAxis
                  tickFormatter={(v: number) => `$${v.toFixed(2)}`}
                  width={70}
                />
                <Tooltip
                  formatter={(value) => {
                    // Recharts widens its formatter param type beyond what
                    // our chart actually emits (number | null). Narrow back
                    // here so the "cost unknown" branch fires for null buckets.
                    if (value === null || value === undefined) {
                      return t("chart.unknown");
                    }
                    if (typeof value === "number") {
                      return `$${value.toFixed(4)}`;
                    }
                    return String(value);
                  }}
                />
                <Legend />
                {/* connectNulls=false so an unpriced bucket renders as
                    a gap, not a misleading interpolated line — operators
                    must see the "cost unknown" hole explicitly. */}
                <Line
                  type="monotone"
                  dataKey="cost_usd"
                  name={t("chart.costPlatform")}
                  stroke="#2563eb"
                  strokeWidth={2}
                  dot={false}
                  connectNulls={false}
                />
                <Line
                  type="monotone"
                  dataKey="cost_usd_byok"
                  name={t("chart.costByok")}
                  stroke="#16a34a"
                  strokeWidth={2}
                  dot={false}
                  connectNulls={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </Section>

      <Section title={t("table.title")}>
        {rangeError !== null ? (
          <p className="text-sm text-gray-500 dark:text-gray-400 p-6">
            {t("rangeErrorPlaceholder")}
          </p>
        ) : loading ? (
          <div className="p-6">
            <TableLoadingState rows={6} />
          </div>
        ) : error ? (
          // Render the same error in both sections so the table never
          // misrepresents a fetch failure as "no data" — empty rows
          // happen by design (cleared on error in catch), so the
          // EmptyState branch alone would conflate failure with
          // genuine zero results.
          <ErrorBanner error={error.message} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={BarChart3}
            title={t("emptyState.title")}
            description={t("emptyState.description")}
          />
        ) : (
          <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("table.periodStart")}</TableHead>
                  {showWorkspaceColumn && (
                    <TableHead>{t("table.workspaceId")}</TableHead>
                  )}
                  <TableHead>{t("table.userId")}</TableHead>
                  <TableHead className="text-right">
                    {t("table.calls")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.tokensIn")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.tokensOut")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.embeddingTokens")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.costPlatform")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.costByok")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((row, i) => (
                  <TableRow
                    key={`${row.period_start}-${row.workspace_id ?? "_"}-${row.user_id}-${i}`}
                  >
                    <TableCell className="font-mono text-sm">
                      {row.period_start}
                    </TableCell>
                    {showWorkspaceColumn && (
                      <TableCell className="font-mono text-xs text-gray-500 dark:text-gray-400">
                        {row.workspace_id ? row.workspace_id.slice(0, 8) : "—"}
                      </TableCell>
                    )}
                    <TableCell className="text-sm">{row.user_id}</TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {row.calls.toLocaleString()}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {row.tokens_in.toLocaleString()}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {row.tokens_out.toLocaleString()}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {row.embedding_tokens.toLocaleString()}
                    </TableCell>
                    <TableCell
                      className="text-right font-mono text-sm"
                      title={
                        row.cost_usd === null
                          ? t("table.costUnknownTooltip")
                          : undefined
                      }
                    >
                      {formatCost(row.cost_usd)}
                    </TableCell>
                    <TableCell
                      className="text-right font-mono text-sm"
                      title={
                        row.cost_usd_byok === null
                          ? t("table.costUnknownTooltip")
                          : undefined
                      }
                    >
                      {formatCost(row.cost_usd_byok)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Section>
    </PageContainer>
  );
}
