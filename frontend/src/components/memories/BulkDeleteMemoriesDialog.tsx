"use client";

/**
 * Delete memories by filter (#1941).
 *
 * Date range / type / tags → a dry run shows how many memories match → the
 * confirm button deletes exactly that filter. One memory at a time is not a
 * realistic way to get a workspace back under its Free capacity.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { forgetBulk, type ForgetBulkParams } from "@/lib/api/memory";

export interface BulkDeleteFilters {
  /** `YYYY-MM-DD`, inclusive, in the viewer's time zone. */
  from: string;
  /** `YYYY-MM-DD`, inclusive, in the viewer's time zone. */
  to: string;
  type: string;
  /** Comma-separated. */
  tags: string;
}

const EMPTY: BulkDeleteFilters = { from: "", to: "", type: "", tags: "" };

/** Local midnight of `day` (+`addDays`) as an ISO instant. */
function localMidnightIso(day: string, addDays = 0): string {
  const d = new Date(`${day}T00:00:00`);
  d.setDate(d.getDate() + addDays);
  return d.toISOString();
}

/**
 * The request filters for the form, or `null` when nothing is set — the
 * server refuses an empty filter too, so the dialog never sends one.
 */
export function toForgetBulkFilters(
  f: BulkDeleteFilters,
): Omit<ForgetBulkParams, "context_id" | "dry_run"> | null {
  const tags = f.tags
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  const out: Omit<ForgetBulkParams, "context_id" | "dry_run"> = {};
  if (f.from) out.created_after = localMidnightIso(f.from);
  // "Through" a day = before the next day's midnight.
  if (f.to) out.created_before = localMidnightIso(f.to, 1);
  if (f.type.trim()) out.type = f.type.trim();
  if (tags.length > 0) out.tags = tags;
  return Object.keys(out).length > 0 ? out : null;
}

/** The earlier of the user's ``created_before`` and the count time (both ISO). */
export function pinCreatedBefore(
  createdBefore: string | undefined,
  countedAt: string | null,
): string | undefined {
  if (!countedAt) return createdBefore;
  if (!createdBefore) return countedAt;
  return new Date(createdBefore) < new Date(countedAt)
    ? createdBefore
    : countedAt;
}

interface BulkDeleteMemoriesDialogProps {
  contextId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called with the number deleted, after the delete succeeded. */
  onDeleted: (deleted: number) => void;
  /**
   * Called when a later batch failed after earlier ones deleted ``deleted``
   * memories. The dialog stays open with the error; the parent refreshes its
   * list (and the capacity banner) but shows no success message.
   */
  onPartial?: (deleted: number) => void;
}

export function BulkDeleteMemoriesDialog({
  contextId,
  open,
  onOpenChange,
  onDeleted,
  onPartial,
}: BulkDeleteMemoriesDialogProps) {
  const t = useTranslations("contextDetail.bulkDelete");
  const [filters, setFilters] = useState<BulkDeleteFilters>(EMPTY);
  const [matched, setMatched] = useState<number | null>(null);
  /**
   * When the count was taken (ISO). The delete is pinned to rows created
   * before it, so memories saved after the user saw the count are not
   * swept up by the confirm.
   */
  const [countedAt, setCountedAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** Memories deleted so far by the current confirm (the server caps a request). */
  const [progress, setProgress] = useState<number | null>(null);
  /** Deleted before a failed batch — shown beside the error. */
  const [partial, setPartial] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const update = (key: keyof BulkDeleteFilters, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    // A changed filter is a different set: the old count no longer applies.
    setMatched(null);
    setError(null);
  };

  const run = async (dryRun: boolean) => {
    const request = toForgetBulkFilters(filters);
    if (!request) {
      setError(t("needFilter"));
      return;
    }
    setBusy(true);
    setError(null);
    setPartial(null);
    let deleted = 0;
    try {
      if (dryRun) {
        const at = new Date().toISOString();
        const res = await forgetBulk({
          context_id: contextId,
          ...request,
          dry_run: true,
        });
        setMatched(res.matched ?? 0);
        setCountedAt(at);
        return;
      }
      const pinned = pinCreatedBefore(request.created_before, countedAt);
      // One request deletes at most 2,000 memories; repeat until none remain.
      // Each request resumes at the previous one's cursor, so rows this
      // member may not delete are not rescanned.
      setProgress(0);
      let cursor: string | undefined;
      for (;;) {
        const res = await forgetBulk({
          context_id: contextId,
          ...request,
          ...(pinned ? { created_before: pinned } : {}),
          dry_run: false,
          ...(cursor ? { cursor } : {}),
        });
        deleted += res.deleted ?? 0;
        setProgress(deleted);
        if (!res.remaining || !res.deleted || !res.next_cursor) break;
        cursor = res.next_cursor;
      }
      onDeleted(deleted);
      setFilters(EMPTY);
      setMatched(null);
    } catch (err) {
      // The error stays in the dialog (frontend.md: errors in a dialog body
      // are an Alert inside it). Batches that did delete are reported beside
      // it and the parent refreshes quietly; the old count no longer applies.
      setError(err instanceof Error ? err.message : t("failed"));
      if (deleted > 0) {
        setPartial(deleted);
        setMatched(null);
        onPartial?.(deleted);
      }
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  const close = (next: boolean) => {
    if (busy) return;
    if (!next) {
      setFilters(EMPTY);
      setMatched(null);
      setError(null);
      setPartial(null);
    }
    onOpenChange(next);
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-red-600 dark:text-red-400" />
            {t("title")}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="bulk-delete-from">{t("from")}</Label>
            <Input
              id="bulk-delete-from"
              type="date"
              value={filters.from}
              onChange={(e) => update("from", e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="bulk-delete-to">{t("to")}</Label>
            <Input
              id="bulk-delete-to"
              type="date"
              value={filters.to}
              onChange={(e) => update("to", e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="bulk-delete-type">{t("type")}</Label>
            <Input
              id="bulk-delete-type"
              value={filters.type}
              placeholder={t("typePlaceholder")}
              onChange={(e) => update("type", e.target.value)}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="bulk-delete-tags">{t("tags")}</Label>
            <Input
              id="bulk-delete-tags"
              value={filters.tags}
              placeholder={t("tagsPlaceholder")}
              onChange={(e) => update("tags", e.target.value)}
            />
          </div>
        </div>

        {matched !== null && progress === null && (
          <p className="text-sm" role="status">
            {t("matched", { count: matched })}
          </p>
        )}
        {progress !== null && (
          <p className="text-sm" role="status">
            {t("progress", { count: progress })}
          </p>
        )}
        {error && (
          <Alert variant="destructive">
            <AlertDescription>
              {error}
              {partial !== null && (
                <span className="block">
                  {t("partial", { count: partial })}
                </span>
              )}
            </AlertDescription>
          </Alert>
        )}

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => close(false)}
            disabled={busy}
          >
            {t("cancel")}
          </Button>
          {matched === null ? (
            <Button onClick={() => void run(true)} disabled={busy}>
              {t("count")}
            </Button>
          ) : (
            <Button
              variant="destructive"
              onClick={() => void run(false)}
              disabled={busy || matched === 0}
            >
              {t("confirm", { count: matched })}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
