"use client";

/**
 * Admin Sleep Report Detail Page
 *
 * View a single Sleep Maintenance run with full phase results and action log.
 * Admin-only page (Issue #179).
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { LoadingState } from "@/components/common/LoadingState";
import { apiClient } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { ArrowLeft } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { formatDateTime } from "@/lib/utils/datetime";

type SleepStatus =
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "rolled_back";

interface SleepReportDetail {
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
  embedding_calls_made: number;
  error_message: string | null;
  edge_discovery_result: Record<string, unknown> | null;
  dedup_result: Record<string, unknown> | null;
  importance_result: Record<string, unknown> | null;
  consolidation_result: Record<string, unknown> | null;
  reindex_result: Record<string, unknown> | null;
}

interface SleepActionItem {
  id: number;
  phase: string;
  action_type: string;
  memory_id: string | null;
  target_id: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

interface SleepReportDetailResponse {
  report: SleepReportDetail;
  actions: SleepActionItem[];
  action_count: number;
}

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

function formatDuration(startedAt: string, completedAt: string | null): string {
  if (!completedAt) return "-";
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSec = seconds % 60;
  return `${minutes}m ${remainSec}s`;
}

export default function AdminSleepReportDetailPage() {
  const params = useParams();
  const reportId = params.reportId as string;
  const t = useTranslations("admin.sleepReports");
  const tCommon = useTranslations("admin.common");
  const locale = useLocale();

  const [detail, setDetail] = useState<SleepReportDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const { toast } = useToast();

  useEffect(() => {
    const loadDetail = async () => {
      try {
        setLoading(true);
        const data = await apiClient.get<SleepReportDetailResponse>(
          `/api/v1/admin/sleep-reports/${reportId}`,
        );
        setDetail(data);
      } catch (error: unknown) {
        const err = error as { status?: number };
        if (err?.status === 404) {
          setNotFound(true);
        } else {
          toast({
            title: tCommon("error"),
            description: t("messages.loadError"),
            variant: "destructive",
          });
        }
      } finally {
        setLoading(false);
      }
    };
    loadDetail();
  }, [reportId, t, tCommon, toast]);

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  if (notFound || !detail) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <div className="p-8 text-center text-gray-500 dark:text-gray-400">
          {t("messages.notFound")}
        </div>
        <Link href="/admin/sleep-reports">
          <Button variant="outline">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("actions.back")}
          </Button>
        </Link>
      </PageContainer>
    );
  }

  const { report, actions, action_count } = detail;

  const phaseResults = [
    { key: "edgeDiscovery", result: report.edge_discovery_result },
    { key: "dedup", result: report.dedup_result },
    { key: "importance", result: report.importance_result },
    { key: "consolidation", result: report.consolidation_result },
    { key: "reindex", result: report.reindex_result },
  ];

  return (
    <PageContainer>
      <PageHeader
        title={t("title")}
        actions={
          <Link href="/admin/sleep-reports">
            <Button variant="outline">
              <ArrowLeft className="h-4 w-4 mr-2" />
              {t("actions.back")}
            </Button>
          </Link>
        }
      />

      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>{t("detail.overview")}</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 text-sm">
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.reportId")}
                </dt>
                <dd className="font-mono text-xs break-all">{report.id}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.status")}
                </dt>
                <dd>
                  <span
                    className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded ${getStatusColor(report.status)}`}
                  >
                    {t(`status.${report.status}`)}
                  </span>
                </dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.userId")}
                </dt>
                <dd className="font-mono text-xs break-all">
                  {report.user_id}
                </dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.contextId")}
                </dt>
                <dd className="font-mono text-xs break-all">
                  {report.context_id || "-"}
                </dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.startedAt")}
                </dt>
                <dd>{formatDateTime(report.started_at, "UTC", locale)}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.completedAt")}
                </dt>
                <dd>
                  {report.completed_at
                    ? formatDateTime(report.completed_at, "UTC", locale)
                    : "-"}
                </dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.duration")}
                </dt>
                <dd>
                  {formatDuration(report.started_at, report.completed_at)}
                </dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.memoriesProcessed")}
                </dt>
                <dd className="font-mono">{report.memories_processed}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.edgesCreated")}
                </dt>
                <dd className="font-mono">{report.edges_created}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.memoriesMerged")}
                </dt>
                <dd className="font-mono">{report.memories_merged}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.memoriesPromoted")}
                </dt>
                <dd className="font-mono">{report.memories_promoted}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.memoriesFlagged")}
                </dt>
                <dd className="font-mono">{report.memories_flagged}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.llmCallsMade")}
                </dt>
                <dd className="font-mono">{report.llm_calls_made}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.llmTokensUsed")}
                </dt>
                <dd className="font-mono">{report.llm_tokens_used}</dd>
              </div>
              <div>
                <dt className="text-gray-500 dark:text-gray-400">
                  {t("detail.embeddingCallsMade")}
                </dt>
                <dd className="font-mono">{report.embedding_calls_made}</dd>
              </div>
              {report.error_message && (
                <div className="md:col-span-2">
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.errorMessage")}
                  </dt>
                  <dd className="text-red-600 dark:text-red-400 font-mono text-xs break-all">
                    {report.error_message}
                  </dd>
                </div>
              )}
            </dl>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>{t("detail.phaseResults")}</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {phaseResults.map(({ key, result }) => (
                <div key={key}>
                  <h4 className="text-sm font-semibold mb-1">
                    {t(`detail.${key}`)}
                  </h4>
                  {result ? (
                    <pre className="text-xs bg-gray-50 dark:bg-gray-800 p-3 rounded overflow-x-auto">
                      {JSON.stringify(result, null, 2)}
                    </pre>
                  ) : (
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      {t("detail.noResults")}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>
              {t("detail.actionLog", { count: action_count })}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {actions.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {t("detail.noActions")}
              </p>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t("detail.phase")}</TableHead>
                      <TableHead>{t("detail.actionType")}</TableHead>
                      <TableHead>{t("detail.memoryId")}</TableHead>
                      <TableHead>{t("detail.targetId")}</TableHead>
                      <TableHead>{t("detail.details")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {actions.map((action) => (
                      <TableRow key={action.id}>
                        <TableCell className="text-xs">
                          {action.phase}
                        </TableCell>
                        <TableCell className="text-xs font-medium">
                          {action.action_type}
                        </TableCell>
                        <TableCell className="font-mono text-xs break-all max-w-[180px]">
                          {action.memory_id || "-"}
                        </TableCell>
                        <TableCell className="font-mono text-xs break-all max-w-[180px]">
                          {action.target_id || "-"}
                        </TableCell>
                        <TableCell className="font-mono text-xs max-w-md">
                          {action.details
                            ? JSON.stringify(action.details)
                            : "-"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </PageContainer>
  );
}
