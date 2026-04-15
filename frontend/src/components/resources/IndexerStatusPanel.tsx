/**
 * IndexerStatusPanel
 *
 * Per-resource indexer observability panel for the Resource Detail Overview
 * tab. Renders four mutually exclusive states — loading / error / empty /
 * data — using the shared primitives from `common/` and `ui/`, intentionally
 * reusing the KpiCard grid shape established by `ResourceStatsStrip` so the
 * detail page stays visually coherent.
 *
 * Issue #326 — closes the UX blind spot from #318 / #320 by making ingest
 * progress, errors, and lag visible to workspace owners without scraping logs.
 */

"use client";

import { useLocale, useTranslations } from "next-intl";
import { Activity, AlertTriangle, CheckCircle2, Clock } from "lucide-react";

import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { KpiCard } from "@/components/ui/kpi-card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useAuth } from "@/contexts/AuthContext";
import type {
  IndexerJobStatus,
  IndexerStatusResponse,
  ResourceEventItem,
} from "@/lib/api/resources";
import { formatRelativeTime } from "@/lib/utils/datetime";

type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

// Lag > 1h is "high" by default — aligns with the typical indexer job cadence
// (runs every few minutes) so a fresh green indexer never trips the threshold
// while a clearly stale one does.
const HIGH_LAG_SECONDS = 60 * 60;

function jobStatusVariant(status: IndexerJobStatus): BadgeVariant {
  switch (status) {
    case "failed":
      return "destructive";
    case "running":
      return "default";
    case "idle":
    case "queued":
    default:
      return "secondary";
  }
}

interface IndexerStatusPanelProps {
  data: IndexerStatusResponse | undefined;
  isLoading: boolean;
  error: Error | null;
}

export function IndexerStatusPanel({
  data,
  isLoading,
  error,
}: IndexerStatusPanelProps) {
  const t = useTranslations("resources");
  const locale = useLocale();
  const { user } = useAuth();
  const timezone = user?.timezone || "UTC";
  const numberFormatter = new Intl.NumberFormat(locale);

  if (isLoading) {
    return (
      <Card
        className="p-6"
        aria-busy="true"
        data-testid="indexer-status-skeleton"
      >
        <TableLoadingState rows={4} />
      </Card>
    );
  }

  if (error) {
    return <ErrorBanner error={error.message || t("indexer.fetchError")} />;
  }

  // "Never run AND no events" is the true empty state. A panel with events
  // but no state (unusual — would mean ingest fired but the indexer never
  // picked the context up) still renders the data branch so the events are
  // at least visible.
  if (!data || (data.state === null && data.recent_events.length === 0)) {
    return (
      <EmptyState
        icon={Activity}
        title={t("indexer.emptyTitle")}
        description={t("indexer.emptyDescription")}
        compact
      />
    );
  }

  const { state, recent_events: events } = data;
  const metrics = state?.metrics;

  return (
    <Card
      className="p-6 space-y-6"
      role="status"
      aria-live="polite"
      aria-label={t("indexer.panelAriaLabel")}
    >
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <div className="rounded-lg border bg-card p-4 space-y-2">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Activity
              className={
                state?.job_status === "failed"
                  ? "h-4 w-4 text-destructive"
                  : "h-4 w-4"
              }
            />
            <span>{t("indexer.jobStatus")}</span>
          </div>
          {state ? (
            <Badge
              variant={jobStatusVariant(state.job_status)}
              data-variant={jobStatusVariant(state.job_status)}
              aria-label={t("indexer.jobStatusLabel", {
                status: t(`indexer.status.${state.job_status}`),
              })}
            >
              {t(`indexer.status.${state.job_status}`)}
            </Badge>
          ) : (
            <p className="text-2xl font-semibold">—</p>
          )}
        </div>
        <KpiCard
          icon={Clock}
          label={t("indexer.lastRun")}
          value={
            state?.last_run_at
              ? formatRelativeTime(state.last_run_at, timezone, locale)
              : "—"
          }
          subtext={
            state?.lag_seconds !== null &&
            state?.lag_seconds !== undefined &&
            state.lag_seconds > HIGH_LAG_SECONDS
              ? t("indexer.lagHigh")
              : undefined
          }
        />
        <KpiCard
          icon={CheckCircle2}
          label={t("indexer.appliedUpserts")}
          value={numberFormatter.format(metrics?.applied_upserts ?? 0)}
          subtext={
            metrics && metrics.applied_deletes > 0
              ? t("indexer.appliedDeletes", {
                  count: metrics.applied_deletes,
                })
              : undefined
          }
        />
        <KpiCard
          icon={AlertTriangle}
          label={t("indexer.errors")}
          value={numberFormatter.format(metrics?.errors ?? 0)}
        />
      </div>

      {metrics?.skipped_reason && (
        <Alert variant="default">
          <AlertDescription>
            {t(`indexer.skipped.${metrics.skipped_reason}`)}
          </AlertDescription>
        </Alert>
      )}

      <RecentEventsTable events={events} timezone={timezone} locale={locale} />
    </Card>
  );
}

interface RecentEventsTableProps {
  events: ResourceEventItem[];
  timezone: string;
  locale: string;
}

function RecentEventsTable({
  events,
  timezone,
  locale,
}: RecentEventsTableProps) {
  const t = useTranslations("resources");

  if (events.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("indexer.noRecentEvents")}
      </p>
    );
  }

  return (
    <div>
      <h3 className="text-sm font-medium mb-2">{t("indexer.recentEvents")}</h3>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("indexer.event.op")}</TableHead>
            <TableHead>{t("indexer.event.docId")}</TableHead>
            <TableHead>{t("indexer.event.version")}</TableHead>
            <TableHead>{t("indexer.event.createdAt")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {events.map((ev) => (
            <TableRow key={ev.id}>
              <TableCell>
                <Badge
                  variant={ev.op === "delete" ? "destructive" : "secondary"}
                  data-op={ev.op}
                >
                  {t(`indexer.event.ops.${ev.op}`)}
                </Badge>
              </TableCell>
              <TableCell className="font-mono text-xs">{ev.doc_id}</TableCell>
              <TableCell>{ev.version ?? "—"}</TableCell>
              <TableCell>
                {ev.created_at
                  ? formatRelativeTime(ev.created_at, timezone, locale)
                  : "—"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
