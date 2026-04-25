"use client";

/**
 * Edit Memory Dialog
 *
 * Form for editing an existing memory via the UUID-addressed
 * `PATCH /api/v1/memory/{memory_id}` endpoint (Issue #439).
 *
 * Replaces the legacy composite-key form. Operates on `MemoryReference`
 * (full detail) and submits only fields that differ from the initial
 * value (client-side dirty detection) so an empty PATCH is never sent.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { updateMemoryById, type UpdateMemoryByIdPatch } from "@/lib/api/memory";
import type { KnownMemoryType, MemoryReference } from "@/lib/types/memory";

interface EditMemoryDialogProps {
  memory: MemoryReference;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: (updated: MemoryReference) => void;
}

// `MemoryType` is a free string at the column level (`String(50)`), but the
// UI ships with two known values for styling/badges. The Select offers these
// two options when the current value matches one of them; an unknown current
// value (e.g. `"code"`, `"decision"`) is shown as read-only with a helper
// note so editing this dialog does not accidentally overwrite values the UI
// has no opinion about. Source of truth is `KnownMemoryType` in `lib/types/memory`.
const KNOWN_TYPES: readonly KnownMemoryType[] = ["normal", "coding"] as const;

function tagsToCsv(tags: string[] | undefined | null): string {
  return (tags ?? []).join(", ");
}

function csvToTags(csv: string): string[] {
  return csv
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

function detailsToText(
  details: Record<string, unknown> | null | undefined,
): string {
  // Distinguish `null`/undefined (no details — empty textarea) from `{}`
  // (explicit empty object — show as "{}" so the dirty check doesn't false-
  // positive convert `{}` to `null` on submit).
  if (details == null) return "";
  return JSON.stringify(details, null, 2);
}

export function EditMemoryDialog({
  memory,
  open,
  onOpenChange,
  onSuccess,
}: EditMemoryDialogProps) {
  const t = useTranslations("contextDetail.editDialog");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [summary, setSummary] = useState(memory.summary);
  const [content, setContent] = useState(memory.content);
  const [type, setType] = useState(memory.type);
  const [importance, setImportance] = useState(String(memory.importance));
  const [tagsCsv, setTagsCsv] = useState(tagsToCsv(memory.tags));
  const [detailsText, setDetailsText] = useState(detailsToText(memory.details));
  const [detailsParseError, setDetailsParseError] = useState<string | null>(
    null,
  );

  // Reset form on (a) memory id changing or (b) the dialog opening. The
  // component stays mounted across open/close cycles; without the `open`
  // dependency, closing mid-edit then reopening on the same memory would
  // resurrect the prior in-progress edits + error state — Cancel is
  // expected to discard, mirroring DeleteMemoryDialog and the rest of the
  // dialog set. The `if (!open) return` guard skips the closing transition.
  useEffect(() => {
    if (!open) return;
    setSummary(memory.summary);
    setContent(memory.content);
    setType(memory.type);
    setImportance(String(memory.importance));
    setTagsCsv(tagsToCsv(memory.tags));
    setDetailsText(detailsToText(memory.details));
    setDetailsParseError(null);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [memory.memory_id, open]);

  const typeIsKnown = useMemo<boolean>(
    () => KNOWN_TYPES.some((t) => t === memory.type),
    [memory.type],
  );

  const handleClose = () => {
    if (loading) return;
    onOpenChange(false);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;

    // Compute dirty fields. Only changed values are included in the patch.
    // Type as `UpdateMemoryByIdPatch` (not `Record<string, unknown>`) so the
    // request body matches the helper's contract and tsc catches typos.
    const patch: UpdateMemoryByIdPatch = {};

    const summaryTrimmed = summary.trim();
    if (summaryTrimmed !== memory.summary) {
      if (summaryTrimmed.length < 10) {
        setError(t("summaryTooShort"));
        return;
      }
      patch.summary = summaryTrimmed;
    }

    if (content !== memory.content) {
      if (content.trim().length === 0) {
        setError(t("contentRequired"));
        return;
      }
      patch.content = content;
    }

    if (typeIsKnown && type !== memory.type) {
      patch.type = type;
    }

    // `Number("") === 0` in JS — without this guard, clearing the importance
    // input then submitting would silently set the column to 0 (and treat it
    // as a real change vs the prior 0.5 default). Treat empty / non-finite
    // as a validation error so the user has to type a number to commit.
    const importanceTrimmed = importance.trim();
    if (importanceTrimmed !== String(memory.importance)) {
      if (importanceTrimmed.length === 0) {
        setError(t("importanceOutOfRange"));
        return;
      }
      const importanceNum = Number(importanceTrimmed);
      if (!Number.isFinite(importanceNum)) {
        setError(t("importanceOutOfRange"));
        return;
      }
      if (importanceNum < 0 || importanceNum > 1) {
        setError(t("importanceOutOfRange"));
        return;
      }
      if (Math.abs(importanceNum - memory.importance) > 1e-9) {
        patch.importance = importanceNum;
      }
    }

    // Order-insensitive tag comparison: backend may normalize tag order on
    // round-trip (PostgreSQL ARRAY ordering is not guaranteed stable across
    // updates), so a positional diff would fire spurious PATCH calls when
    // the user types the same set in a different order.
    const newTags = csvToTags(tagsCsv);
    const currentTags = memory.tags ?? [];
    const newTagsSorted = [...newTags].sort();
    const currentTagsSorted = [...currentTags].sort();
    const tagsDiffer =
      newTagsSorted.length !== currentTagsSorted.length ||
      newTagsSorted.some((tag, idx) => tag !== currentTagsSorted[idx]);
    if (tagsDiffer) {
      patch.tags = newTags;
    }

    let parsedDetails: Record<string, unknown> | null | undefined;
    const detailsTrimmed = detailsText.trim();
    if (detailsTrimmed.length === 0) {
      parsedDetails = null;
    } else {
      try {
        const parsed = JSON.parse(detailsTrimmed);
        if (
          typeof parsed !== "object" ||
          parsed === null ||
          Array.isArray(parsed)
        ) {
          setDetailsParseError(t("detailsMustBeObject"));
          return;
        }
        parsedDetails = parsed as Record<string, unknown>;
      } catch {
        setDetailsParseError(t("detailsParseError"));
        return;
      }
    }
    setDetailsParseError(null);
    // Canonical (no pretty-print) JSON for the dirty check so round-tripped
    // objects with different key spacing don't fire false positives. The
    // human-readable pretty-print is only for the textarea display.
    const currentDetailsCanonical = JSON.stringify(memory.details ?? null);
    const newDetailsCanonical = JSON.stringify(parsedDetails ?? null);
    if (newDetailsCanonical !== currentDetailsCanonical) {
      patch.details = parsedDetails ?? null;
    }

    if (Object.keys(patch).length === 0) {
      setError(t("noChanges"));
      return;
    }

    try {
      setLoading(true);
      setError(null);
      const updated = await updateMemoryById(memory.memory_id, patch);
      onSuccess(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("failed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t("title")}</DialogTitle>
          <DialogDescription>
            {t("description", { id: memory.memory_id })}
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {/* Summary */}
          <div className="space-y-2">
            <Label htmlFor="edit-summary">
              {t("summaryLabel")} <span className="text-red-500">*</span>
            </Label>
            <Textarea
              id="edit-summary"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              rows={2}
              maxLength={500}
              required
            />
          </div>

          {/* Content */}
          <div className="space-y-2">
            <Label htmlFor="edit-content">
              {t("contentLabel")} <span className="text-red-500">*</span>
            </Label>
            <Textarea
              id="edit-content"
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={6}
              required
            />
          </div>

          {/* Type — Select for known values, read-only display otherwise */}
          <div className="space-y-2">
            <Label htmlFor="edit-type">{t("typeLabel")}</Label>
            {typeIsKnown ? (
              <Select value={type} onValueChange={setType}>
                <SelectTrigger id="edit-type">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="normal">{t("typeNormal")}</SelectItem>
                  <SelectItem value="coding">{t("typeCoding")}</SelectItem>
                </SelectContent>
              </Select>
            ) : (
              <div className="space-y-1">
                <Input id="edit-type" value={memory.type} disabled />
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  {t("typeUnknownReadonly")}
                </p>
              </div>
            )}
          </div>

          {/* Importance + Tags side-by-side */}
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="edit-importance">{t("importanceLabel")}</Label>
              <Input
                id="edit-importance"
                type="number"
                min="0"
                max="1"
                step="0.1"
                value={importance}
                onChange={(e) => setImportance(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="edit-tags">{t("tagsLabel")}</Label>
              <Input
                id="edit-tags"
                value={tagsCsv}
                onChange={(e) => setTagsCsv(e.target.value)}
                placeholder={t("tagsPlaceholder")}
              />
            </div>
          </div>

          {/* Details — JSON */}
          <div className="space-y-2">
            <Label htmlFor="edit-details">{t("detailsLabel")}</Label>
            <Textarea
              id="edit-details"
              value={detailsText}
              onChange={(e) => {
                setDetailsText(e.target.value);
                if (detailsParseError) setDetailsParseError(null);
              }}
              rows={4}
              placeholder={t("detailsPlaceholder")}
              className="font-mono text-xs"
            />
            {detailsParseError && (
              <p className="text-xs text-red-600 dark:text-red-400">
                {detailsParseError}
              </p>
            )}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={handleClose}
              disabled={loading}
            >
              {t("cancel")}
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? t("saving") : t("confirm")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
