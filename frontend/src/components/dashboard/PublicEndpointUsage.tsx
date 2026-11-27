"use client";

/**
 * Public endpoint usage card (#3035).
 *
 * The owner's statistics of the resource's live public MCP endpoint: calls,
 * searches and answered searches over 7 or 30 UTC days, a chart per UTC day,
 * searches by outcome and the documents searches returned most often. The
 * notes under the numbers say which traffic is covered and what is not
 * counted. Mounted only for a workspace owner of a context with a live
 * endpoint (`shouldShowPublicEndpointUsage`); a 404 renders nothing.
 */

import { useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Globe } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LoadingState } from "@/components/common/LoadingState";
import { ApiError } from "@/lib/api/base";
import {
  getPublicMcpUsageStats,
  type PublicMcpUsageStatsResponse,
} from "@/lib/api/resources";
import { WorkspaceRole, hasWorkspaceRole } from "@/lib/auth/rbac";

/** The order the outcomes are listed in (the server's outcome set). */
const OUTCOME_ORDER = [
  "success",
  "degraded",
  "denied",
  "rate_limited",
  "invalid_request",
  "error",
  "cancelled",
] as const;

/** Whether the owner card is mounted: an owner, a live public endpoint, a resource. */
export function shouldShowPublicEndpointUsage(
  context: {
    public_mcp_endpoint_live?: boolean;
    resource_id?: string | null;
  },
  role: WorkspaceRole | string | null | undefined,
): boolean {
  return (
    context.public_mcp_endpoint_live === true &&
    !!context.resource_id &&
    hasWorkspaceRole(role, WorkspaceRole.Owner)
  );
}

interface PublicEndpointUsageProps {
  resourceId: string;
  /** Bumped by the panel's Refresh button. */
  refreshKey: number;
}

type Loaded =
  | { kind: "ok"; data: PublicMcpUsageStatsResponse }
  | { kind: "not_found" }
  | { kind: "rate_limited" }
  | { kind: "error" };

type LoadState = Loaded | { kind: "loading" };

function Tile({ label, value }: { label: string; value: number }) {
  return (
    <div
      className="space-y-1"
      data-testid="public-endpoint-tile"
      data-label={label}
      data-value={String(value)}
    >
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold">{value.toLocaleString()}</p>
    </div>
  );
}

export function PublicEndpointUsage({
  resourceId,
  refreshKey,
}: PublicEndpointUsageProps) {
  const t = useTranslations("contextStats");
  const locale = useLocale();
  const [days, setDays] = useState<7 | 30>(7);
  // The answer of one request; a request that has not answered is loading.
  const requestKey = `${resourceId}|${days}|${refreshKey}`;
  const [answer, setAnswer] = useState<{ key: string; loaded: Loaded } | null>(
    null,
  );
  const state: LoadState =
    answer?.key === requestKey ? answer.loaded : { kind: "loading" };

  useEffect(() => {
    let cancelled = false;
    const settle = (loaded: Loaded) => {
      if (!cancelled) setAnswer({ key: requestKey, loaded });
    };
    getPublicMcpUsageStats(resourceId, days)
      .then((data) => settle({ kind: "ok", data }))
      .catch((error: unknown) => {
        const status = error instanceof ApiError ? error.status : undefined;
        if (status === 404) settle({ kind: "not_found" });
        else if (status === 429) settle({ kind: "rate_limited" });
        else settle({ kind: "error" });
      });
    return () => {
      cancelled = true;
    };
  }, [resourceId, days, refreshKey, requestKey]);

  if (state.kind === "not_found") return null;

  // A UTC calendar day, never shifted to the viewer's time zone.
  const dayLabel = (day: string) =>
    new Intl.DateTimeFormat(locale, {
      month: "numeric",
      day: "numeric",
      timeZone: "UTC",
    }).format(new Date(`${day}T00:00:00Z`));

  const header = (
    <CardHeader>
      <div className="flex items-center justify-between gap-4">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Globe className="h-5 w-5" />
            {t("publicEndpoint.title")}
          </CardTitle>
          <CardDescription>{t("publicEndpoint.description")}</CardDescription>
        </div>
        <div className="flex gap-2">
          {([7, 30] as const).map((n) => (
            <Button
              key={n}
              variant={days === n ? "default" : "outline"}
              size="sm"
              onClick={() => setDays(n)}
            >
              {n}
              {t("days")}
            </Button>
          ))}
        </div>
      </div>
    </CardHeader>
  );

  let body;
  if (state.kind === "loading") {
    body = <LoadingState lines={4} />;
  } else if (state.kind === "rate_limited") {
    body = (
      <p className="text-sm text-muted-foreground">
        {t("publicEndpoint.rateLimited")}
      </p>
    );
  } else if (state.kind === "error") {
    body = <ErrorBanner error={t("publicEndpoint.loadFailed")} />;
  } else {
    const { data } = state;
    const docs = data.documents;
    const answered =
      (data.totals.search_outcomes.success ?? 0) +
      (data.totals.search_outcomes.degraded ?? 0);
    const outcomes = OUTCOME_ORDER.filter(
      (o) => (data.totals.search_outcomes[o] ?? 0) > 0,
    );
    body =
      data.totals.calls === 0 ? (
        <EmptyState
          compact
          icon={Globe}
          title={t("publicEndpoint.emptyTitle")}
          description={t("publicEndpoint.emptyDescription")}
        />
      ) : (
        <div className="space-y-6">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <Tile label={t("publicEndpoint.calls")} value={data.totals.calls} />
            <Tile
              label={t("publicEndpoint.searches")}
              value={data.totals.searches}
            />
            <Tile label={t("publicEndpoint.searchesAnswered")} value={answered} />
          </div>

          <div className="space-y-2">
            <h4 className="font-semibold text-sm">
              {t("publicEndpoint.chartTitle")}
            </h4>
            <ResponsiveContainer width="100%" height={250}>
              <LineChart
                data={data.days}
                aria-label={t("publicEndpoint.chartTitle")}
              >
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis
                  dataKey="date"
                  tickFormatter={dayLabel}
                  className="text-xs"
                />
                <YAxis className="text-xs" allowDecimals={false} />
                <Tooltip
                  labelFormatter={dayLabel}
                  formatter={(value: number, name: string) => [
                    value,
                    name === "calls"
                      ? t("publicEndpoint.calls")
                      : t("publicEndpoint.searches"),
                  ]}
                />
                <Line
                  type="monotone"
                  dataKey="calls"
                  name="calls"
                  stroke="#10b981"
                  strokeWidth={2}
                />
                <Line
                  type="monotone"
                  dataKey="searches"
                  name="searches"
                  stroke="#3b82f6"
                  strokeWidth={2}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="space-y-2">
            <h4 className="font-semibold text-sm">
              {t("publicEndpoint.outcomesTitle")}
            </h4>
            {outcomes.length > 0 ? (
              <ul
                className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm"
                data-testid="public-endpoint-outcomes"
              >
                {outcomes.map((o) => (
                  <li key={o} className="flex justify-between gap-2">
                    <span className="text-muted-foreground">
                      {t(`publicEndpoint.outcomes.${o}`)}
                    </span>
                    <span className="font-semibold">
                      {(data.totals.search_outcomes[o] ?? 0).toLocaleString()}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">
                {t("publicEndpoint.noSearches")}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <h4 className="font-semibold text-sm">
              {t("publicEndpoint.documentsTitle")}
            </h4>
            {docs.status === "unavailable" ? (
              <p className="text-sm text-muted-foreground">
                {t("publicEndpoint.documentsUnavailable")}
              </p>
            ) : !docs.publication_serving ? (
              <p className="text-sm text-muted-foreground">
                {t("publicEndpoint.notServing")}
              </p>
            ) : docs.items.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {t("publicEndpoint.noDocuments")}
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("publicEndpoint.document")}</TableHead>
                    <TableHead className="text-right">
                      {t("publicEndpoint.searches")}
                    </TableHead>
                    <TableHead className="text-right">
                      {t("publicEndpoint.bestRank")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {docs.items.map((item) => (
                    <TableRow key={item.doc_key}>
                      <TableCell>
                        {item.title === null ? (
                          <span className="text-muted-foreground italic">
                            {t("publicEndpoint.noLongerPublished")}
                          </span>
                        ) : item.source_url ? (
                          <a
                            href={item.source_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-600 hover:underline dark:text-blue-400"
                          >
                            {item.title}
                          </a>
                        ) : (
                          item.title
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        {item.searches.toLocaleString()}
                      </TableCell>
                      <TableCell className="text-right">
                        {item.best_rank ?? "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {docs.status === "ok" &&
              docs.searches_scanned >= docs.scan_limit && (
                <p className="text-xs text-muted-foreground">
                  {t("publicEndpoint.scanLimitReached", {
                    count: docs.scan_limit,
                  })}
                </p>
              )}
          </div>
        </div>
      );
  }

  const recordingOff = state.kind === "ok" && !state.data.recording_enabled;
  const retentionDays = state.kind === "ok" ? state.data.retention_days : null;

  return (
    <Card>
      {header}
      <CardContent className="space-y-4">
        {body}
        {recordingOff && (
          <p className="text-sm text-amber-700 dark:text-amber-400">
            {t("publicEndpoint.recordingOff")}
          </p>
        )}
        {retentionDays !== null && (
          <ul
            className="list-disc pl-5 space-y-1 text-xs text-muted-foreground"
            data-testid="public-endpoint-notes"
          >
            <li>
              {t("publicEndpoint.scopeNote", { section: t("publicAPIUsage") })}
            </li>
            <li>{t("publicEndpoint.cacheNote")}</li>
            <li>{t("publicEndpoint.callsNote")}</li>
            <li>
              {t("publicEndpoint.retentionNote", { days: retentionDays })}
            </li>
            <li>{t("publicEndpoint.undercountNote")}</li>
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
