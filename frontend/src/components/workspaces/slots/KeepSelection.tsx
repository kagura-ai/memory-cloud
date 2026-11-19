"use client";

/**
 * "Which workspaces do you want to keep using?" (#2919).
 *
 * A fieldset of checkboxes, at most `max` checked. At the cap the unchecked
 * boxes are disabled, which takes them out of the tab order, so the live
 * count also says what the cap is. Preview shows which workspaces stay active
 * and which will be suspended; Save becomes available once the current choice
 * was previewed, and sends `expected_version`.
 */

import { useId, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
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
  slotsFullDetails,
} from "./slotErrors";
import { SlotsFullRemedies } from "./SlotsFullRemedies";

export interface KeepSelectionProps {
  scenario: WorkspaceSlotScenario;
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

export function KeepSelection({
  scenario,
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
  const [selected, setSelected] = useState<string[]>(initial);
  const [preview, setPreview] = useState<WorkspaceSlotPreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "save" | null>(null);
  const [full, setFull] = useState<WorkspaceSlotsFullDetails | null>(null);

  const names = useMemo(
    () => new Map(workspaces.map((w) => [w.id, w.name])),
    [workspaces],
  );
  const atCap = selected.length >= max;

  const toggle = (id: string, checked: boolean) => {
    setPreview(null);
    setFull(null);
    setSelected((prev) =>
      checked
        ? prev.includes(id) || prev.length >= max
          ? prev
          : [...prev, id]
        : prev.filter((x) => x !== id),
    );
  };

  const runPreview = async () => {
    setBusy("preview");
    setFull(null);
    try {
      setPreview(await previewWorkspaceSlots(selected, scenario));
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
    setBusy("save");
    try {
      const next = await saveWorkspaceSelection(selected, version);
      toast({ title: t("keep.saved") });
      onSaved(next);
    } catch (error) {
      if (errorCode(error) === SLOT_STALE) {
        onStale();
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

  const list = (ids: string[]) =>
    ids.length === 0 ? (
      <p className="text-sm text-muted-foreground">{t("keep.none")}</p>
    ) : (
      <ul className="list-disc space-y-0.5 pl-5 text-sm">
        {ids.map((id) => (
          <li key={id}>{names.get(id) ?? id}</li>
        ))}
      </ul>
    );

  const legendId = `${baseId}-legend`;
  const unmet = preview?.unmet ?? [];
  const willBeSuspended = (preview?.suspended ?? []).filter(
    (id) => !unmet.includes(id),
  );

  return (
    <div className="space-y-4">
      <fieldset aria-labelledby={legendId} className="space-y-2">
        <legend id={legendId} className="mb-2 font-medium">
          {t("keep.question")}
        </legend>
        {workspaces.map((w) => {
          const id = `${baseId}-${w.id}`;
          const checked = selected.includes(w.id);
          const locked = lockedIds.includes(w.id);
          return (
            <div key={w.id} className="flex items-start gap-3">
              <Checkbox
                id={id}
                checked={checked}
                disabled={locked || (!checked && atCap) || busy === "save"}
                onCheckedChange={(value) => toggle(w.id, value === true)}
                className="mt-0.5"
              />
              <div className="min-w-0">
                <Label htmlFor={id} className="flex flex-wrap items-center gap-2">
                  <span className="break-words">{w.name}</span>
                  <PlanBadge planName={w.plan_name} size="sm" />
                </Label>
                {locked && (
                  <p className="text-xs text-muted-foreground">
                    {t("keep.contracted")}
                  </p>
                )}
              </div>
            </div>
          );
        })}
      </fieldset>

      <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
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
          disabled={busy !== null || preview === null}
          aria-busy={busy === "save"}
        >
          {busy === "save" && <InlineSpinner size="sm" className="mr-2" />}
          {busy === "save" ? t("keep.saving") : t("keep.save")}
        </Button>
      </div>

      {preview && (
        <section
          aria-label={t("keep.previewTitle")}
          className="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-700"
        >
          <h3 className="font-medium">{t("keep.previewTitle")}</h3>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <h4 className="mb-1 text-sm font-medium">{t("keep.staysActive")}</h4>
              {list(preview.active)}
            </div>
            <div>
              <h4 className="mb-1 text-sm font-medium">
                {t("keep.willBeSuspended")}
              </h4>
              {list(willBeSuspended)}
            </div>
          </div>
          {unmet.length > 0 && (
            <div>
              <h4 className="mb-1 text-sm font-medium">
                {t("keep.staysSuspended")}
              </h4>
              {list(unmet)}
            </div>
          )}
        </section>
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
