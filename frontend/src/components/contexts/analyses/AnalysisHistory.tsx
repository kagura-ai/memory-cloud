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
  // Issue #732: when ``onSelectRun`` is provided, rows become clickable to view
  // that run's results; ``selectedRunId`` marks the row currently being viewed.
  selectedRunId?: string | null;
  onSelectRun?: (runId: string) => void;
}

// Translatable status labels — keyed by the canonical
// ``ANALYSIS_STATUSES`` enum from ``lib/api/analyses``. Anything
// outside the enum (e.g. a future ``timeout`` taxonomy) falls back
// to the ``unknown`` key with the raw status interpolated.
const KNOWN_STATUSES = new Set(["running", "succeeded", "failed", "cancelled"]);

export function AnalysisHistory({
  runs,
  total,
  activeRunId,
  selectedRunId = null,
  onSelectRun,
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
            const isSelected = selectedRunId === run.run_id;
            const clickable = !!onSelectRun;
            const handleSelect = () => onSelectRun?.(run.run_id);
            return (
              <TableRow
                key={run.run_id}
                onClick={clickable ? handleSelect : undefined}
                onKeyDown={
                  clickable
                    ? (e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          handleSelect();
                        }
                      }
                    : undefined
                }
                tabIndex={clickable ? 0 : undefined}
                role={clickable ? "button" : undefined}
                aria-current={isSelected ? "true" : undefined}
                aria-label={
                  clickable
                    ? t("viewRunAria", {
                        when: formatLocalDate(new Date(run.started_at)),
                      })
                    : undefined
                }
                className={[
                  isSelected
                    ? "bg-blue-50 ring-1 ring-inset ring-blue-300 dark:bg-blue-900/30 dark:ring-blue-700"
                    : isActive
                      ? "bg-emerald-50/40 dark:bg-emerald-900/20"
                      : clickable
                        ? "hover:bg-gray-50 dark:hover:bg-gray-800/50"
                        : "",
                  clickable ? "cursor-pointer" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <TableCell className="font-mono text-xs">
                  {formatLocalDate(new Date(run.started_at))}
                </TableCell>
                <TableCell className="text-xs text-gray-600 dark:text-gray-400">
                  {run.status === "cancelled"
                    ? t("status.cancelled", {
                        reason: run.cancellation_reason ?? "",
                      })
                    : KNOWN_STATUSES.has(run.status)
                      ? t(
                          `status.${run.status}` as
                            | "status.running"
                            | "status.succeeded"
                            | "status.failed",
                        )
                      : t("status.unknown", { raw: run.status })}
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
