"use client";

/**
 * Shared Sleep Reports List Component
 *
 * Issue #526: Rendered by both ``/admin/sleep-reports`` (cross-workspace)
 * and ``/workspace/sleep-reports`` (single-workspace, owner/admin scoped).
 *
 * The two pages differ in their fetcher function and whether the "Run Now"
 * action is shown — everything else (table, filters, pagination) is
 * identical and lives here.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations, useLocale } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import {
  LoadingState,
  TableLoadingState,
  InlineSpinner,
} from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import {
  RefreshCw,
  Eye,
  ChevronLeft,
  ChevronRight,
  Play,
  Moon,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils/datetime";
import {
  getSleepStatusColor,
  SLEEP_STATUS_OPTIONS,
  type SleepStatus,
} from "@/lib/sleep-report";
import type {
  SleepReportSummary,
  SleepReportListResponse,
} from "@/lib/api/sleep-reports";

const PAGE_SIZE = 50;

export interface SleepReportsListFetchParams {
  status?: SleepStatus;
  limit?: number;
  offset?: number;
  user_id?: string;
  context_id?: string;
}

export type SleepReportsListFetcher = (
  params: SleepReportsListFetchParams,
) => Promise<SleepReportListResponse>;

export interface SleepReportsListProps {
  title: string;
  description: string;
  fetchData: SleepReportsListFetcher;
  detailHrefPrefix: string;
  translationNamespace?: string;
  showRunNow?: boolean;
  onRunNow?: () => Promise<void>;
  running?: boolean;
  ready?: boolean;
}

export function SleepReportsList({
  title,
  description,
  fetchData,
  detailHrefPrefix,
  translationNamespace = "admin.sleepReports",
  showRunNow = false,
  onRunNow,
  running = false,
  ready = true,
}: SleepReportsListProps) {
  const t = useTranslations(translationNamespace);
  const tCommon = useTranslations("admin.common");
  const locale = useLocale();
  const { toast } = useToast();

  const [reports, setReports] = useState<SleepReportSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selectedStatus, setSelectedStatus] = useState<SleepStatus | null>(
    null,
  );
  const [offset, setOffset] = useState(0);

  const loadReports = useCallback(async () => {
    if (!ready) return;
    try {
      setLoading(true);
      const data = await fetchData({
        status: selectedStatus ?? undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setReports(data.reports);
      setTotal(data.total);
    } catch {
      toast({
        title: tCommon("error"),
        description: t("messages.loadError"),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  }, [offset, selectedStatus, fetchData, ready, toast, t, tCommon]);

  useEffect(() => {
    loadReports();
  }, [loadReports]);

  const handleRunNow = useCallback(async () => {
    if (!onRunNow) return;
    try {
      await onRunNow();
      await loadReports();
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : null;
      if (apiErr?.status === 409) {
        const runningReportId = apiErr.details?.running_report_id as
          | string
          | undefined;
        toast({
          title: t("messages.runConflict"),
          description: runningReportId ? (
            <Link
              href={`${detailHrefPrefix}/${runningReportId}`}
              className="underline underline-offset-2"
            >
              {t("messages.runConflictViewLink")}
            </Link>
          ) : undefined,
          variant: "destructive",
        });
      } else {
        toast({
          title: tCommon("error"),
          description:
            err instanceof Error ? err.message : t("messages.runError"),
          variant: "destructive",
        });
      }
    }
  }, [onRunNow, loadReports, toast, t, tCommon, detailHrefPrefix]);

  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + PAGE_SIZE, total);
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  if (!ready) {
    return (
      <PageContainer>
        <PageHeader title={title} description={description} />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={title}
        description={description}
        actions={
          <>
            <Button
              onClick={loadReports}
              variant="outline"
              disabled={loading || running}
            >
              {loading ? (
                <InlineSpinner size="sm" className="mr-2" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-2" />
              )}
              {t("actions.refresh")}
            </Button>
            {showRunNow && onRunNow && (
              <Button onClick={handleRunNow} disabled={running || loading}>
                {running ? (
                  <InlineSpinner size="sm" className="mr-2" />
                ) : (
                  <Play className="h-4 w-4 mr-2" />
                )}
                {running ? t("actions.runNowPending") : t("actions.runNow")}
              </Button>
            )}
          </>
        }
      />

      <Section title={t("filter.title")}>
        <div className="flex flex-wrap gap-2 mb-4">
          <button
            type="button"
            aria-pressed={selectedStatus === null}
            className={`inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-blue-500 dark:focus-visible:ring-offset-gray-900 ${
              selectedStatus === null
                ? "bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900 border-transparent"
                : "border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
            }`}
            onClick={() => {
              setSelectedStatus(null);
              setOffset(0);
            }}
          >
            {t("filter.all", { count: total })}
          </button>
          {SLEEP_STATUS_OPTIONS.map((status) => (
            <button
              key={status}
              type="button"
              aria-pressed={selectedStatus === status}
              className={`inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-blue-500 dark:focus-visible:ring-offset-gray-900 ${
                selectedStatus === status
                  ? `${getSleepStatusColor(status)} border-transparent`
                  : "border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
              }`}
              onClick={() => {
                setSelectedStatus(status);
                setOffset(0);
              }}
            >
              {t(`status.${status}`)}
            </button>
          ))}
        </div>
      </Section>

      <Section title={t("title")}>
        <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
          {loading ? (
            <div className="p-6">
              <TableLoadingState rows={5} />
            </div>
          ) : reports.length === 0 ? (
            <EmptyState
              icon={Moon}
              title={t("messages.noReports")}
              description={t("messages.noReportsHint")}
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("table.startedAt")}</TableHead>
                  <TableHead>{t("table.context")}</TableHead>
                  <TableHead>{t("table.status")}</TableHead>
                  <TableHead className="text-right">
                    {t("table.memoriesProcessed")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.edgesCreated")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.merges")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.promotions")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.llmCalls")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("table.actions")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {reports.map((report) => (
                  <TableRow key={report.id}>
                    <TableCell className="text-sm whitespace-nowrap">
                      {formatRelativeTime(report.started_at, locale)}
                    </TableCell>
                    <TableCell className="text-sm">
                      {report.context_name ?? "—"}
                    </TableCell>
                    <TableCell>
                      <span
                        className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded ${getSleepStatusColor(report.status)}`}
                      >
                        {t(`status.${report.status}`)}
                      </span>
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {report.memories_processed}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {report.edges_created}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {report.memories_merged}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {report.memories_promoted}
                    </TableCell>
                    <TableCell className="text-right font-mono text-sm">
                      {report.llm_calls_made}
                    </TableCell>
                    <TableCell className="text-right">
                      <Link href={`${detailHrefPrefix}/${report.id}`}>
                        <Button variant="ghost" size="sm">
                          <Eye className="h-4 w-4 mr-1" />
                          {t("actions.viewDetail")}
                        </Button>
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>

        {total > 0 && (
          <div className="flex items-center justify-between mt-4">
            <div className="text-sm text-gray-600 dark:text-gray-400">
              {t("pagination.showing", {
                from: pageStart,
                to: pageEnd,
                total,
              })}
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!hasPrev || loading}
                onClick={() =>
                  setOffset((prev) => Math.max(0, prev - PAGE_SIZE))
                }
              >
                <ChevronLeft className="h-4 w-4 mr-1" />
                {t("pagination.previous")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!hasNext || loading}
                onClick={() => setOffset((prev) => prev + PAGE_SIZE)}
              >
                {t("pagination.next")}
                <ChevronRight className="h-4 w-4 ml-1" />
              </Button>
            </div>
          </div>
        )}
      </Section>
    </PageContainer>
  );
}
