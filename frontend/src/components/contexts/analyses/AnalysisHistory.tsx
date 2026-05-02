"use client";

/**
 * AnalysisHistory — past run table for the analyses tab (Issue #497).
 *
 * Renders the paginated ``listAnalysisRuns`` payload as a small read-
 * only table. Sticky-NULL on cost columns: an unpriced run shows "—"
 * via ``formatCostCents`` rather than a misleading $0.000.
 */

import { useTranslations } from "next-intl";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { History } from "lucide-react";
import { formatLocalDate } from "@/lib/utils/datetime";
import { formatCostCents } from "./analysisFormatters";
import type { AnalysisRunRow } from "@/lib/api/analyses";

interface AnalysisHistoryProps {
  runs: AnalysisRunRow[];
  total: number | null;
  activeRunId: string | null;
}

function statusLabel(run: AnalysisRunRow): string {
  if (run.cancellation_reason) return `cancelled · ${run.cancellation_reason}`;
  return run.status;
}

export function AnalysisHistory({
  runs,
  total,
  activeRunId,
}: AnalysisHistoryProps) {
  const t = useTranslations("analyses.history");

  if (runs.length === 0) {
    return (
      <EmptyState
        compact
        icon={History}
        title={t("noRunsTitle")}
        description={t("noRunsDescription")}
      />
    );
  }

  return (
    <div className="rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <header className="border-b border-gray-100 px-5 py-4 dark:border-gray-800">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
          {t("title")}
        </h3>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          {total !== null && total !== runs.length
            ? t("subtitle", { visible: runs.length, total })
            : t("subtitleAtMost", { visible: runs.length })}
        </p>
      </header>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("headerDate")}</TableHead>
            <TableHead>{t("headerStatus")}</TableHead>
            <TableHead className="text-right">{t("headerMemories")}</TableHead>
            <TableHead className="text-right">{t("headerCost")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => {
            const isActive = activeRunId === run.run_id;
            return (
              <TableRow
                key={run.run_id}
                className={
                  isActive ? "bg-emerald-50/40 dark:bg-emerald-900/20" : ""
                }
              >
                <TableCell className="font-mono text-xs">
                  {formatLocalDate(new Date(run.started_at))}
                </TableCell>
                <TableCell className="text-xs text-gray-600 dark:text-gray-400">
                  {statusLabel(run)}
                </TableCell>
                <TableCell className="text-right font-medium">
                  {run.input_count.toLocaleString()}
                </TableCell>
                <TableCell className="text-right">
                  {formatCostCents(
                    run.cost_actual_cents ?? run.cost_estimated_cents,
                  )}
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
