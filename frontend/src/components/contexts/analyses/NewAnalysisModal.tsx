"use client";

/**
 * NewAnalysisModal — pre-flight + start trigger for a new run (Issue #497).
 *
 * Filters: ``period`` (from / to), ``types`` (single select),
 * ``min_importance`` (range slider), ``query`` (text). The ``tags``
 * field is supported on the API surface but the modal does not yet
 * expose a chip selector — follow-up work, see
 * ``analyses.modal.tags*`` i18n keys reserved for that UI.
 * Runs ``previewAnalysis`` whenever the form changes (debounced) so the
 * preflight strip shows live ``estimated_cost_cents`` and
 * ``memory_count``.
 *
 * Submit posts ``startAnalysis`` and on 202 calls ``onStarted(run_id)``
 * so the parent can pivot into "watching the active run" mode.
 *
 * URL state: this modal is driven by ``?new=1`` on the URL — opening
 * and closing is owned by the parent (AnalysesTabPanel) which sets /
 * removes the query param. The modal itself is a pure
 * controlled component.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { InlineSpinner } from "@/components/common/LoadingState";
import { CheckCircle2 } from "lucide-react";
import { ApiError } from "@/lib/api/base";
import {
  previewAnalysis,
  startAnalysis,
  type AnalysisFilters,
  type AnalysisPreviewResponse,
} from "@/lib/api/analyses";
import { formatLocalDate } from "@/lib/utils/datetime";
import { formatCostCents } from "./analysisFormatters";

interface NewAnalysisModalProps {
  open: boolean;
  contextId: string;
  contextName: string;
  onClose: () => void;
  onStarted: (runId: string) => void;
}

const DEFAULT_TYPES = [
  "all",
  "decision",
  "pattern",
  "note",
  "feature-design",
  "learning",
] as const;

// ``toISOString().slice(0, 10)`` shifts the date for users east/west
// of UTC (e.g. JST users at 23:30 see "tomorrow"). ``formatLocalDate``
// uses local-tz components — the same helper the cost dashboard uses
// for ``<input type="date">`` defaults (#473 / PR #527 convention).
function todayIso(): string {
  return formatLocalDate(new Date());
}

function isoDaysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return formatLocalDate(d);
}

export function NewAnalysisModal({
  open,
  contextId,
  contextName,
  onClose,
  onStarted,
}: NewAnalysisModalProps) {
  const t = useTranslations("analyses.modal");
  // ``actions.cancel`` lives one namespace up; a separate hook reaches it
  // without re-keying the modal namespace.
  const tActions = useTranslations("analyses.actions");

  const [from, setFrom] = useState(() => isoDaysAgo(30));
  const [to, setTo] = useState(() => todayIso());
  const [type, setType] = useState<string>("all");
  const [minImportance, setMinImportance] = useState(50);
  const [query, setQuery] = useState("");

  const [preview, setPreview] = useState<AnalysisPreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Build filter payload from form. Memo so the preview effect's deps
  // are content-stable.
  const filters = useMemo<AnalysisFilters>(() => {
    const out: AnalysisFilters = { from, to };
    if (type !== "all") out.types = [type];
    if (minImportance > 0) out.min_importance = minImportance / 100;
    if (query.trim().length > 0) out.query = query.trim();
    return out;
  }, [from, to, type, minImportance, query]);

  // Debounced preview: 300ms after the form settles. Cancels a stale
  // request when the user keeps typing.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewError(null);
    const timer = window.setTimeout(async () => {
      try {
        const result = await previewAnalysis(contextId, filters);
        if (cancelled) return;
        setPreview(result);
      } catch (err) {
        if (cancelled) return;
        setPreviewError(
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : "preview failed",
        );
      } finally {
        if (!cancelled) setPreviewLoading(false);
      }
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [open, contextId, filters]);

  // Reset state when modal closes so the next open starts fresh.
  useEffect(() => {
    if (!open) {
      setSubmitting(false);
      setSubmitError(null);
    }
  }, [open]);

  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const res = await startAnalysis(contextId, filters);
      onStarted(res.run_id);
    } catch (err) {
      setSubmitError(
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "start failed",
      );
    } finally {
      setSubmitting(false);
    }
  }, [contextId, filters, onStarted]);

  const previewMemoryCount = preview?.memory_count ?? null;
  const previewCostCents = preview?.estimated_cost_cents ?? null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>
            {t("subtitle", { contextName })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div>
            <Label>{t("period")}</Label>
            <div className="mt-1.5 grid grid-cols-2 gap-2">
              <Input
                type="date"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
              />
              <Input
                type="date"
                value={to}
                onChange={(e) => setTo(e.target.value)}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <Label>{t("types")}</Label>
              <Select value={type} onValueChange={setType}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DEFAULT_TYPES.map((opt) => (
                    <SelectItem key={opt} value={opt}>
                      {opt === "all" ? t("typesAll") : opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>
                {t("minImportance")} ·{" "}
                <span className="font-mono text-emerald-700 dark:text-emerald-400">
                  {(minImportance / 100).toFixed(2)}
                </span>
              </Label>
              <input
                type="range"
                min={0}
                max={100}
                value={minImportance}
                onChange={(e) => setMinImportance(Number(e.target.value))}
                className="mt-3 h-2 w-full accent-gray-900 dark:accent-gray-100"
                aria-label={t("minImportance")}
              />
            </div>
          </div>

          <div>
            <Label>
              {t("narrativeQuery")}{" "}
              <span className="font-normal text-gray-400">
                {t("narrativeQueryHint")}
              </span>
            </Label>
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("narrativeQueryPlaceholder")}
              className="mt-1.5"
            />
          </div>
        </div>

        <div className="rounded-md border border-gray-200 bg-gray-50 px-4 py-3 text-sm dark:border-gray-700 dark:bg-gray-800/50">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-xs text-gray-500 dark:text-gray-400">
                {t("preflight.memoriesCount")}
              </div>
              <div className="font-semibold text-gray-900 dark:text-gray-100">
                {previewLoading || previewMemoryCount === null
                  ? t("preflight.loading")
                  : previewMemoryCount.toLocaleString()}
              </div>
            </div>
            <div>
              <div className="text-xs text-gray-500 dark:text-gray-400">
                {t("preflight.estimatedCost")}
              </div>
              <div className="font-semibold text-emerald-700 dark:text-emerald-400">
                {previewLoading
                  ? t("preflight.loading")
                  : formatCostCents(previewCostCents)}
              </div>
            </div>
            <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
          </div>
          {previewError && (
            <p className="mt-2 text-xs text-red-600 dark:text-red-400">
              {t("errorLoadingPreview")}: {previewError}
            </p>
          )}
        </div>

        {submitError && (
          <Alert variant="destructive">
            <AlertDescription>
              {t("errorStarting")}: {submitError}
            </AlertDescription>
          </Alert>
        )}

        <DialogFooter>
          <p className="mr-auto text-xs text-gray-500 dark:text-gray-400">
            {t("footerHint")}
          </p>
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            {tActions("cancel")}
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={submitting || previewLoading || preview === null}
          >
            {submitting ? (
              <>
                <InlineSpinner size="sm" />
                <span className="ml-2">{t("submitting")}</span>
              </>
            ) : (
              t("submit")
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
