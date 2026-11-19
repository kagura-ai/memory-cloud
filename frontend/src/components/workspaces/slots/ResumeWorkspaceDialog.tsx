"use client";

/**
 * Resume a suspended workspace (#2919).
 *
 * Previews the `current` scenario with the workspace first and the active
 * ones after it, then says what happens: a free slot, or which workspace is
 * suspended instead. Confirming saves that same order with the version the
 * dialog opened on: the order and the version are frozen when it opens, so a
 * refetch while it is open never changes what is confirmed (a server state
 * that moved on answers SLOT-003). When no slot can be freed (every active
 * slot is under a paid subscription) the dialog shows the remedies instead
 * of a confirm button. The caller mounts one instance per opening.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { LoadingState } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";
import {
  previewWorkspaceSlots,
  saveWorkspaceSelection,
  type SlotWorkspace,
  type WorkspaceSlotPreview,
  type WorkspaceSlots,
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

/** The workspace first, then the active ones in the saved order. */
function resumeOrder(workspaceId: string, slots: WorkspaceSlots): string[] {
  const active = slots.workspaces
    .filter((w) => w.status === "active")
    .map((w) => w.id);
  const saved = slots.selection.workspace_ids.filter((id) => active.includes(id));
  const rest = active.filter((id) => !saved.includes(id));
  return [workspaceId, ...saved, ...rest];
}

export function ResumeWorkspaceDialog({
  workspace,
  slots,
  onClose,
  onResumed,
  onStale,
  onCloseAutoFocus,
}: {
  /** The suspended workspace; null = closed. */
  workspace: SlotWorkspace | null;
  slots: WorkspaceSlots;
  onClose: () => void;
  onResumed: (data: WorkspaceSlots) => void;
  onStale: () => void;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const t = useTranslations("workspaceSlots");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  // Frozen at mount (one mount per opening).
  const [snapshot] = useState(() =>
    workspace
      ? {
          id: workspace.id,
          name: workspace.name,
          order: resumeOrder(workspace.id, slots),
          version: slots.version,
          nextTier: slots.allowance.next_tier,
          capOnNextTier: slots.allowance.cap_on_next_tier,
        }
      : null,
  );
  const [preview, setPreview] = useState<WorkspaceSlotPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [full, setFull] = useState<WorkspaceSlotsFullDetails | null>(null);
  const [saving, setSaving] = useState(false);

  const names = useMemo(
    () => new Map(slots.workspaces.map((w) => [w.id, w.name])),
    [slots.workspaces],
  );

  useEffect(() => {
    if (!snapshot) return;
    let alive = true;
    previewWorkspaceSlots(snapshot.order, "current")
      .then((res) => {
        if (!alive) return;
        if (res.unmet.includes(snapshot.id)) {
          setFull({
            next_tier: snapshot.nextTier,
            cap_on_next_tier: snapshot.capOnNextTier,
          });
        } else {
          setPreview(res);
        }
      })
      .catch((e) => {
        if (alive) setError(errorMessage(e, t("resume.failed")));
      });
    return () => {
      alive = false;
    };
    // One preview per opening: `snapshot` never changes after mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snapshot]);

  const close = () => {
    if (saving) return;
    onClose();
  };

  const confirm = async () => {
    if (!snapshot) return;
    setSaving(true);
    setError(null);
    try {
      const next = await saveWorkspaceSelection(snapshot.order, snapshot.version);
      toast({ title: t("resume.resumed", { name: snapshot.name }) });
      setSaving(false);
      onResumed(next);
    } catch (e) {
      setSaving(false);
      if (errorCode(e) === SLOT_STALE) {
        setPreview(null);
        setError(t("stale"));
        onStale();
        return;
      }
      // The preview stays and Resume stays enabled for the retry.
      if (isSlotsBusy(e)) {
        setError(t("busy"));
        return;
      }
      const details = slotsFullDetails(e);
      if (details) {
        setPreview(null);
        setFull(details);
        return;
      }
      setError(errorMessage(e, t("resume.failed")));
    }
  };

  const name = snapshot?.name ?? "";
  const suspendInstead = preview?.changes.suspend ?? [];
  const checking = preview === null && error === null && full === null;

  return (
    <AlertDialog open={workspace !== null} onOpenChange={(open) => !open && close()}>
      <AlertDialogContent onCloseAutoFocus={onCloseAutoFocus}>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("resume.title", { name })}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2" aria-live="polite">
              {checking && (
                <>
                  <p>{t("resume.checking")}</p>
                  <LoadingState lines={2} />
                </>
              )}
              {preview && suspendInstead.length === 0 && (
                <p>{t("resume.freeSlot", { name })}</p>
              )}
              {preview && suspendInstead.length > 0 && (
                <>
                  <p>{t("resume.swap", { name })}</p>
                  <ul className="list-inside list-disc space-y-1 text-sm">
                    {suspendInstead.map((id) => (
                      <li key={id}>{names.get(id) ?? id}</li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {full && (
          <SlotsFullRemedies
            nextTier={full.next_tier}
            capOnNextTier={full.cap_on_next_tier}
          />
        )}
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={saving}>{tCommon("cancel")}</AlertDialogCancel>
          {preview && (
            <Button type="button" onClick={() => void confirm()} disabled={saving}>
              {saving ? t("resume.resuming") : t("resume.confirm")}
            </Button>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
