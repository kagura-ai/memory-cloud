"use client";

import * as React from "react";
import { useMemo, useState } from "react";
import Link from "next/link";
import { useTranslations, useLocale } from "next-intl";
import { useAuth } from "@/contexts/AuthContext";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { ErrorBanner } from "@/components/common/ErrorBanner";
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
  AlertTriangle,
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
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  formatDateTime,
  formatDuration,
  formatRelativeTime,
} from "@/lib/utils/datetime";
import {
  buildHeadline,
  buildJudgeFailureNote,
  buildPhaseNarrative,
  type PhaseName,
} from "@/lib/utils/sleep-narrative";
import { getSleepStatusColor } from "@/lib/sleep-report";
import type { SleepReportDetailResponse } from "@/lib/api/sleep-reports";

function PhaseResultJson({ result }: { result: object }): React.ReactElement {
  const json = useMemo(() => JSON.stringify(result, null, 2), [result]);
  return (
    <pre className="text-xs bg-gray-50 dark:bg-gray-800 px-3 py-2 overflow-x-auto border-t border-gray-200 dark:border-gray-700">
      {json}
    </pre>
  );
}

function PhaseIcon({
  result,
}: {
  result: { success: boolean; skipped: boolean; error: string | null } | null;
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

export interface SleepReportDetailViewProps {
  detail: SleepReportDetailResponse;
  backHref: string;
  backLabel: string;
  translationNamespace?: string;
}

export function SleepReportDetailView({
  detail,
  backHref,
  backLabel,
  translationNamespace = "admin.sleepReports",
}: SleepReportDetailViewProps) {
  const t = useTranslations(translationNamespace);
  const locale = useLocale();
  const { user } = useAuth();
  const timezone = user?.timezone || "UTC";
  const [showMetadata, setShowMetadata] = useState(false);

  const { report, actions, action_count } = detail;

  const phaseResults: {
    key: PhaseName;
    result: typeof report.edge_discovery_result;
  }[] = [
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
          <Link href={backHref}>
            <Button variant="outline">
              <ArrowLeft className="h-4 w-4 mr-2" />
              {backLabel}
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

        {report.status === "degraded" && (
          // #1183/#1190: degraded = run finished but SOME judge-LLM calls
          // failed. Informational (not an error) — an ErrorBanner would
          // overstate it, but badge-only understates it.
          <Alert
            // Informational, not an error — role="status" (polite) instead of
            // the Alert default role="alert" (assertive) so screen readers
            // don't announce it with error urgency (#1190 review).
            role="status"
            className="border-yellow-300 bg-yellow-50 text-yellow-800 dark:border-yellow-700 dark:bg-yellow-900/20 dark:text-yellow-300 [&>svg]:text-yellow-600 dark:[&>svg]:text-yellow-400"
          >
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>
              {t("detail.narrative.runDegraded", {
                count: report.llm_call_failures ?? 0,
              })}
            </AlertDescription>
          </Alert>
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
              const judgeNote = buildJudgeFailureNote(result);
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
                      {judgeNote && (
                        <div className="text-xs text-yellow-700 dark:text-yellow-400 truncate">
                          {t(judgeNote.key, judgeNote.values)}
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
                    <PhaseResultJson result={result} />
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
