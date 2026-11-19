"use client";

/**
 * "Which workspaces do you want to keep using?" (#2919).
 *
 * A fieldset of checkboxes, at most `max` checked. At the cap the unchecked
 * boxes are disabled, which takes them out of the tab order, so the count —
 * which the fieldset is described by — says what the cap is. Preview shows
 * the outcome; Save is available only while the shown preview is of the
 * current selection (design 12.7), and sends `expected_version`.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { InlineSpinner } from "@/components/common/LoadingState";
import { PlanBadge } from "@/components/common/PlanBadge";
import { useToast } from "@/hooks/use-toast";
import {
  previewWorkspaceSlots,
  saveWorkspaceSelection,
  type SlotWorkspace,
  type WorkspaceSlotPreview,
  type WorkspaceSlots,
  type WorkspaceSlotScenario,
  type WorkspaceSlotsFullDetails,
} from "@/lib/api/workspaceSlots";
import {
  SLOT_STALE,
  errorCode,
  errorMessage,
  isSlotsBusy,
  slotsFullDetails,
} from "./slotErrors";
import { SlotsFullRemedies } from "./SlotsFullRemedies";

interface KeepSelectionProps {
  scenario: WorkspaceSlotScenario;
  /** The question the fieldset asks. */
  legend: string;
  workspaces: SlotWorkspace[];
  /** Checked at first, in priority order. */
  initial: string[];
  /** How many may be checked. */
  max: number;
  /** Always kept and not changeable here (a running paid subscription). */
  lockedIds?: string[];
  version: number;
  onSaved: (data: WorkspaceSlots) => void;
  /** The server answered SLOT-003: the page refetches. */
  onStale: () => void;
}

/** A preview and the selection it was asked for. */
interface ShownPreview {
  forKey: string;
  result: WorkspaceSlotPreview;
}

const keyOf = (ids: readonly string[]) => ids.join(",");

export function KeepSelection({
  scenario,
  legend,
  workspaces,
  initial,
  max,
  lockedIds = [],
  version,
  onSaved,
  onStale,
}: KeepSelectionProps) {
  const t = useTranslations("workspaceSlots");
  const { toast } = useToast();
  const baseId = useId();
  const previewHeading = useRef<HTMLHeadingElement>(null);
  const [selected, setSelected] = useState<string[]>(initial);
  const [preview, setPreview] = useState<ShownPreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "save" | null>(null);
  const [full, setFull] = useState<WorkspaceSlotsFullDetails | null>(null);
  const [slotsBusy, setSlotsBusy] = useState(false);

  const names = useMemo(
    () => new Map(workspaces.map((w) => [w.id, w.name])),
    [workspaces],
  );
  const selectedKey = keyOf(selected);
  // A response for an earlier selection is kept but never shown or saved.
  const current = preview?.forKey === selectedKey ? preview.result : null;
  const atCap = selected.length >= max;

  useEffect(() => {
    if (preview && preview.forKey === keyOf(selected)) {
      previewHeading.current?.focus();
    }
    // Only a new preview moves focus; a toggle does not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preview]);

  const toggle = (id: string, checked: boolean) => {
    setFull(null);
    setSlotsBusy(false);
    setSelected((prev) =>
      checked
        ? prev.includes(id) || prev.length >= max
          ? prev
          : [...prev, id]
        : prev.filter((x) => x !== id),
    );
  };

  const runPreview = async () => {
    const asked = [...selected];
    setBusy("preview");
    setFull(null);
    try {
      const result = await previewWorkspaceSlots(asked, scenario);
      setPreview({ forKey: keyOf(asked), result });
    } catch (error) {
      toast({
        variant: "destructive",
        title: t("keep.previewFailed"),
        description: errorMessage(error, t("keep.previewFailed")),
      });
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (current === null) return;
    setBusy("save");
    setSlotsBusy(false);
    try {
      const next = await saveWorkspaceSelection(selected, version);
      toast({ title: t("keep.saved") });
      onSaved(next);
    } catch (error) {
      if (errorCode(error) === SLOT_STALE) {
        onStale();
        return;
      }
      // Selection and preview stay; Save stays enabled for the retry.
      if (isSlotsBusy(error)) {
        setSlotsBusy(true);
        return;
      }
      const details = slotsFullDetails(error);
      if (details) {
        setFull(details);
        return;
      }
      toast({
        variant: "destructive",
        title: t("keep.saveFailed"),
        description: errorMessage(error, t("keep.saveFailed")),
      });
    } finally {
      setBusy(null);
    }
  };

  const ids = {
    legend: `${baseId}-legend`,
    count: `${baseId}-count`,
    hint: `${baseId}-hint`,
    previewTitle: `${baseId}-preview`,
  };

  const outcome = (heading: string, id: string, list: string[]) => (
    <div>
      <h4 id={id} className="mb-1 text-sm font-medium">
        {heading}
      </h4>
      {list.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("keep.none")}</p>
      ) : (
        <ul aria-labelledby={id} className="list-disc space-y-0.5 pl-5 text-sm">
          {list.map((wid) => (
            <li key={wid}>{names.get(wid) ?? wid}</li>
          ))}
        </ul>
      )}
    </div>
  );

  // Design 12.7: `active` / `suspended` are the complete sets after applying,
  // `changes` the differences. "No slot can be freed" only in `current`.
  const unmet = scenario === "current" ? (current?.unmet ?? []) : [];
  const willBeSuspended = current?.changes.suspend ?? [];
  const staysSuspended = (current?.suspended ?? []).filter(
    (id) => !willBeSuspended.includes(id) && !unmet.includes(id),
  );

  return (
    <div className="space-y-4">
      <fieldset
        aria-labelledby={ids.legend}
        aria-describedby={ids.count}
        className="space-y-3"
      >
        <legend id={ids.legend} className="mb-2 font-medium">
          {legend}
        </legend>
        <div className="space-y-2">
          {workspaces.map((w) => {
            const id = `${baseId}-${w.id}`;
            const noteId = `${id}-note`;
            const checked = selected.includes(w.id);
            const locked = lockedIds.includes(w.id);
            return (
              <div key={w.id} className="flex items-start gap-3">
                <Checkbox
                  id={id}
                  checked={checked}
                  disabled={locked || (!checked && atCap) || busy === "save"}
                  onCheckedChange={(value) => toggle(w.id, value === true)}
                  aria-describedby={locked ? noteId : undefined}
                  className="mt-0.5"
                />
                <div className="min-w-0">
                  <Label htmlFor={id} className="flex flex-wrap items-center gap-2">
                    <span className="break-words">{w.name}</span>
                    <PlanBadge planName={w.plan_name} size="sm" />
                  </Label>
                  {locked && (
                    <p id={noteId} className="text-xs text-muted-foreground">
                      {t("keep.contracted")}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        <p
          id={ids.count}
          role="status"
          aria-live="polite"
          className="text-sm text-muted-foreground"
        >
          {t("keep.count", { count: selected.length, max })}
        </p>

        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => void runPreview()}
            disabled={busy !== null}
            aria-busy={busy === "preview"}
          >
            {busy === "preview" && <InlineSpinner size="sm" className="mr-2" />}
            {t("keep.preview")}
          </Button>
          <Button
            type="button"
            onClick={() => void save()}
            disabled={busy !== null || current === null}
            aria-busy={busy === "save"}
            aria-describedby={current === null ? ids.hint : undefined}
          >
            {busy === "save" && <InlineSpinner size="sm" className="mr-2" />}
            {busy === "save" ? t("keep.saving") : t("keep.save")}
          </Button>
        </div>
        {current === null && (
          <p id={ids.hint} className="text-xs text-muted-foreground">
            {t("keep.previewHint")}
          </p>
        )}
      </fieldset>

      {current && (
        <section
          aria-labelledby={ids.previewTitle}
          className="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-700"
        >
          <h3
            id={ids.previewTitle}
            ref={previewHeading}
            tabIndex={-1}
            className="font-medium focus:outline-none"
          >
            {t("keep.previewTitle")}
          </h3>
          <div className="grid gap-4 sm:grid-cols-3">
            {outcome(t("keep.staysActive"), `${ids.previewTitle}-active`, current.active)}
            {outcome(
              t("keep.willBeSuspended"),
              `${ids.previewTitle}-suspend`,
              willBeSuspended,
            )}
            {outcome(
              t("keep.staysSuspended"),
              `${ids.previewTitle}-stays`,
              staysSuspended,
            )}
          </div>
          {unmet.length > 0 &&
            outcome(t("keep.unmet"), `${ids.previewTitle}-unmet`, unmet)}
        </section>
      )}

      {slotsBusy && (
        <Alert variant="warning">
          <AlertDescription>{t("busy")}</AlertDescription>
        </Alert>
      )}

      {full && (
        <SlotsFullRemedies
          nextTier={full.next_tier}
          capOnNextTier={full.cap_on_next_tier}
        />
      )}
    </div>
  );
}
