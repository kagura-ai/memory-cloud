"use client";

/**
 * Admin Sleep Report Detail Page
 *
 * View a single Sleep Maintenance run with phase summaries and action log.
 * Admin-only page (Issue #179).
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { LoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
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
import {
  ArrowLeft,
  CheckCircle2,
  XCircle,
  MinusCircle,
  Clock,
  Database,
  Link2,
  Merge,
  TrendingUp,
  Sparkles,
} from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { formatDateTime, formatRelativeTime } from "@/lib/utils/datetime";
import {
  buildHeadline,
  buildPhaseNarrative,
  type PhaseName,
} from "@/lib/utils/sleep-narrative";
import { getSleepStatusColor, type SleepStatus } from "@/lib/sleep-report";

interface SleepReportDetail {
  id: string;
  user_id: string;
  workspace_id: string | null;
  context_id: string | null;
  context_name: string | null;
  context_deleted: boolean;
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
  edge_discovery_result: PhaseResult | null;
  dedup_result: PhaseResult | null;
  importance_result: PhaseResult | null;
  consolidation_result: PhaseResult | null;
  reindex_result: PhaseResult | null;
}

interface PhaseResult {
  success: boolean;
  skipped: boolean;
  skip_reason: string | null;
  error: string | null;
  llm_calls: number;
  memories_processed: number;
  details: Record<string, unknown> | null;
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

function formatDuration(startedAt: string, completedAt: string | null): string {
  if (!completedAt) return "-";
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSec = seconds % 60;
  return `${minutes}m ${remainSec}s`;
}

function PhaseIcon({
  result,
}: {
  result: PhaseResult | null;
}): React.ReactElement {
  if (!result) {
    return <MinusCircle className="h-5 w-5 text-gray-400" />;
  }
  if (result.skipped) {
    return <MinusCircle className="h-5 w-5 text-gray-400" />;
  }
  if (result.error || !result.success) {
    return <XCircle className="h-5 w-5 text-red-500" />;
  }
  return <CheckCircle2 className="h-5 w-5 text-green-500" />;
}

function KpiTile({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ElementType;
  label: string;
  value: number;
}): React.ReactElement {
  return (
    <div className="flex items-center gap-3 p-4 bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
      <div className="flex-shrink-0 h-10 w-10 rounded-lg bg-gray-100 dark:bg-gray-800 flex items-center justify-center">
        <Icon className="h-5 w-5 text-gray-600 dark:text-gray-400" />
      </div>
      <div className="min-w-0">
        <div className="text-2xl font-semibold text-gray-900 dark:text-gray-100 tabular-nums">
          {value.toLocaleString()}
        </div>
        <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
          {label}
        </div>
      </div>
    </div>
  );
}

export default function AdminSleepReportDetailPage() {
  const params = useParams();
  const reportId = Array.isArray(params.reportId)
    ? params.reportId[0]
    : (params.reportId ?? "");
  const t = useTranslations("admin.sleepReports");
  const locale = useLocale();
  const { user } = useAuth();
  const timezone = user?.timezone || "UTC";

  const [detail, setDetail] = useState<SleepReportDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showMetadata, setShowMetadata] = useState(false);

  useEffect(() => {
    const loadDetail = async () => {
      try {
        setLoading(true);
        setLoadError(null);
        setNotFound(false);
        const data = await apiClient.get<SleepReportDetailResponse>(
          `/api/v1/admin/sleep-reports/${reportId}`,
        );
        setDetail(data);
      } catch (error: unknown) {
        const err = error as { status?: number };
        if (err?.status === 404) {
          setNotFound(true);
        } else {
          setLoadError(t("messages.loadError"));
        }
      } finally {
        setLoading(false);
      }
    };
    loadDetail();
    // t is stable across renders; only reportId should trigger refetch
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reportId]);

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <LoadingState lines={5} />
      </PageContainer>
    );
  }

  if (loadError) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} />
        <ErrorBanner error={loadError} />
        <Link href="/admin/sleep-reports">
          <Button variant="outline">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("actions.back")}
          </Button>
        </Link>
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

  const phaseResults: { key: PhaseName; result: PhaseResult | null }[] = [
    { key: "edgeDiscovery", result: report.edge_discovery_result },
    { key: "dedup", result: report.dedup_result },
    { key: "importance", result: report.importance_result },
    { key: "consolidation", result: report.consolidation_result },
    { key: "reindex", result: report.reindex_result },
  ];

  const headline = buildHeadline(
    {
      context_name: report.context_name,
      context_deleted: report.context_deleted,
    },
    report,
  );

  const relativeStarted = formatRelativeTime(report.started_at, locale);
  const duration = formatDuration(report.started_at, report.completed_at);

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
        {(report.status === "failed" || report.error_message) && (
          <ErrorBanner
            error={
              report.error_message
                ? t("detail.narrative.runFailed", {
                    error: report.error_message,
                  })
                : t(`status.${report.status}`)
            }
          />
        )}

        <div className="p-6 bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div className="flex items-center gap-3">
              <span
                className={`inline-flex items-center px-3 py-1 text-sm font-medium rounded ${getSleepStatusColor(report.status)}`}
              >
                {t(`status.${report.status}`)}
              </span>
              <div>
                <div className="text-sm text-gray-500 dark:text-gray-400">
                  {relativeStarted} · {duration}
                </div>
                <div className="text-xs text-gray-400 dark:text-gray-500">
                  {formatDateTime(report.started_at, timezone, locale)}
                </div>
              </div>
            </div>
          </div>
          <div className="mt-4 text-sm text-gray-800 dark:text-gray-100">
            {t(headline.key, headline.values)}
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
          <KpiTile
            icon={Database}
            label={t("detail.memoriesProcessed")}
            value={report.memories_processed}
          />
          <KpiTile
            icon={Link2}
            label={t("detail.edgesCreated")}
            value={report.edges_created}
          />
          <KpiTile
            icon={Merge}
            label={t("detail.memoriesMerged")}
            value={report.memories_merged}
          />
          <KpiTile
            icon={TrendingUp}
            label={t("detail.memoriesPromoted")}
            value={report.memories_promoted}
          />
          <KpiTile
            icon={Sparkles}
            label={t("detail.llmCallsMade")}
            value={report.llm_calls_made}
          />
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{t("detail.phaseResults")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {phaseResults.map(({ key, result }) => {
              const narrative = buildPhaseNarrative(key, result);
              return (
                <details
                  key={key}
                  className="group rounded border border-gray-200 dark:border-gray-700"
                >
                  <summary className="flex items-center gap-3 px-3 py-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50 list-none">
                    <PhaseIcon result={result} />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                        {t(`detail.${key}`)}
                      </div>
                      {narrative && (
                        <div className="text-xs text-gray-500 dark:text-gray-400 truncate">
                          {t(narrative.key, narrative.values)}
                        </div>
                      )}
                    </div>
                    {result && (
                      <div className="text-xs text-gray-400 dark:text-gray-500 tabular-nums">
                        {result.llm_calls > 0 && `${result.llm_calls} LLM`}
                      </div>
                    )}
                  </summary>
                  {result ? (
                    <pre className="text-xs bg-gray-50 dark:bg-gray-800 px-3 py-2 overflow-x-auto border-t border-gray-200 dark:border-gray-700">
                      {JSON.stringify(result, null, 2)}
                    </pre>
                  ) : (
                    <div className="text-xs text-gray-500 dark:text-gray-400 px-3 py-2 border-t border-gray-200 dark:border-gray-700">
                      {t("detail.noResults")}
                    </div>
                  )}
                </details>
              );
            })}
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
              <div className="text-center py-6">
                <Clock className="h-8 w-8 text-gray-300 dark:text-gray-600 mx-auto mb-2" />
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  {t("detail.noActions")}
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                  {t("detail.noActionsHint")}
                </p>
              </div>
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

        <div>
          <button
            type="button"
            onClick={() => setShowMetadata((v) => !v)}
            className="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded px-2 py-1"
          >
            {showMetadata
              ? `▼ ${t("detail.metadata")}`
              : `▶ ${t("detail.metadata")}`}
          </button>
          {showMetadata && (
            <div className="mt-2 p-4 bg-gray-50 dark:bg-gray-800/50 rounded-lg border border-gray-200 dark:border-gray-700">
              <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-xs">
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.reportId")}
                  </dt>
                  <dd className="font-mono break-all">{report.id}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.userId")}
                  </dt>
                  <dd className="font-mono break-all">{report.user_id}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.contextId")}
                  </dt>
                  <dd className="font-mono break-all">
                    {report.context_id || "-"}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.workspaceId")}
                  </dt>
                  <dd className="font-mono break-all">
                    {report.workspace_id || "-"}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.startedAt")}
                  </dt>
                  <dd>{formatDateTime(report.started_at, timezone, locale)}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.completedAt")}
                  </dt>
                  <dd>
                    {report.completed_at
                      ? formatDateTime(report.completed_at, timezone, locale)
                      : "-"}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.memoriesFlagged")}
                  </dt>
                  <dd className="font-mono">{report.memories_flagged}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.llmTokensUsed")}
                  </dt>
                  <dd className="font-mono">
                    {report.llm_tokens_used.toLocaleString()}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">
                    {t("detail.embeddingCallsMade")}
                  </dt>
                  <dd className="font-mono">{report.embedding_calls_made}</dd>
                </div>
              </dl>
            </div>
          )}
        </div>
      </div>
    </PageContainer>
  );
}
