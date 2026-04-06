"use client";

/**
 * Admin Sleep Reports List Page
 *
 * View Sleep Maintenance execution history.
 * Admin-only page (Issue #179).
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useTranslations, useLocale } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import { LoadingState, InlineSpinner } from "@/components/common/LoadingState";
import { apiClient } from "@/lib/api";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { RefreshCw, Eye, ChevronLeft, ChevronRight } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { formatRelativeTime } from "@/lib/utils/datetime";

type SleepStatus =
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "rolled_back";

interface SleepReportSummary {
  id: string;
  user_id: string;
  workspace_id: string | null;
  context_id: string | null;
  status: SleepStatus;
  started_at: string;
  completed_at: string | null;
  memories_processed: number;
  edges_created: number;
  memories_merged: number;
  memories_promoted: number;
  memories_flagged: number;
  llm_calls_made: number;
  llm_tokens_used: number;
}

interface SleepReportListResponse {
  reports: SleepReportSummary[];
  total: number;
  limit: number;
  offset: number;
}

const PAGE_SIZE = 50;

const STATUS_OPTIONS: SleepStatus[] = [
  "completed",
  "running",
  "failed",
  "cancelled",
  "rolled_back",
];

function getStatusColor(status: SleepStatus): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300";
    case "running":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300";
    case "failed":
      return "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300";
    case "rolled_back":
      return "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300";
    case "cancelled":
    default:
      return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300";
  }
}

export default function AdminSleepReportsPage() {
  const t = useTranslations("admin.sleepReports");
  const tCommon = useTranslations("admin.common");
  const locale = useLocale();

  const [reports, setReports] = useState<SleepReportSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selectedStatus, setSelectedStatus] = useState<SleepStatus | null>(
    null,
  );
  const [offset, setOffset] = useState(0);
  const { toast } = useToast();

  const loadReports = async () => {
    try {
      setLoading(true);
      const params = new URLSearchParams();
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(offset));
      if (selectedStatus) {
        params.set("status", selectedStatus);
      }
      const data = await apiClient.get<SleepReportListResponse>(
        `/api/v1/admin/sleep-reports?${params.toString()}`,
      );
      setReports(data.reports);
      setTotal(data.total);
    } catch (error) {
      toast({
        title: tCommon("error"),
        description: t("messages.loadError"),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadReports();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset, selectedStatus]);

  const statusCounts = useMemo(() => {
    const counts: Record<SleepStatus, number> = {
      running: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
      rolled_back: 0,
    };
    for (const r of reports) {
      counts[r.status] = (counts[r.status] || 0) + 1;
    }
    return counts;
  }, [reports]);

  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + PAGE_SIZE, total);
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  return (
    <PageContainer>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <Button onClick={loadReports} variant="outline" disabled={loading}>
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
          {STATUS_OPTIONS.map((status) => (
            <button
              key={status}
              type="button"
              aria-pressed={selectedStatus === status}
              className={`inline-flex items-center px-3 py-1.5 text-sm font-medium rounded-md border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-blue-500 dark:focus-visible:ring-offset-gray-900 ${
                selectedStatus === status
                  ? `${getStatusColor(status)} border-transparent`
                  : "border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
              }`}
              onClick={() => {
                setSelectedStatus(status);
                setOffset(0);
              }}
            >
              {t("filter.status", {
                status: t(`status.${status}`),
                count: statusCounts[status] || 0,
              })}
            </button>
          ))}
        </div>
      </Section>

      <Section title={t("title")}>
        <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
          {loading ? (
            <div className="p-6">
              <LoadingState lines={5} />
            </div>
          ) : reports.length === 0 ? (
            <div className="p-8 text-center text-gray-500 dark:text-gray-400">
              {t("messages.noReports")}
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("table.startedAt")}</TableHead>
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
                      {formatRelativeTime(report.started_at, "UTC", locale)}
                    </TableCell>
                    <TableCell>
                      <span
                        className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded ${getStatusColor(report.status)}`}
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
                      <Link href={`/admin/sleep-reports/${report.id}`}>
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
