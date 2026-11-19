"use client";

/**
 * Resume a suspended workspace (#2919).
 *
 * Previews the `current` scenario with the workspace first and the active
 * ones after it, then says what happens: a free slot, or which workspace is
 * suspended instead. Confirming saves that same order. When no slot can be
 * freed (every active slot is under a paid subscription) the dialog shows the
 * remedies instead of a confirm button. The caller keys it by the workspace
 * id, so each workspace starts from a fresh state.
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
  slotsFullDetails,
} from "./slotErrors";
import { SlotsFullRemedies } from "./SlotsFullRemedies";

export function ResumeWorkspaceDialog({
  workspace,
  slots,
  onClose,
  onResumed,
  onStale,
}: {
  /** The suspended workspace; null = closed. */
  workspace: SlotWorkspace | null;
  slots: WorkspaceSlots;
  onClose: () => void;
  onResumed: (data: WorkspaceSlots) => void;
  onStale: () => void;
}) {
  const t = useTranslations("workspaceSlots");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const [preview, setPreview] = useState<WorkspaceSlotPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [full, setFull] = useState<WorkspaceSlotsFullDetails | null>(null);
  const [saving, setSaving] = useState(false);

  // The workspace first, then the active ones in the saved order.
  const order = useMemo(() => {
    if (!workspace) return [];
    const active = slots.workspaces
      .filter((w) => w.status === "active")
      .map((w) => w.id);
    const saved = slots.selection.workspace_ids.filter((id) =>
      active.includes(id),
    );
    const rest = active.filter((id) => !saved.includes(id));
    return [workspace.id, ...saved, ...rest];
  }, [workspace, slots]);
  const names = useMemo(
    () => new Map(slots.workspaces.map((w) => [w.id, w.name])),
    [slots.workspaces],
  );

  const workspaceId = workspace?.id;
  useEffect(() => {
    if (!workspaceId) return;
    let alive = true;
    previewWorkspaceSlots(order, "current")
      .then((res) => {
        if (!alive) return;
        if (res.unmet.includes(workspaceId)) {
          setFull({
            next_tier: slots.allowance.next_tier,
            cap_on_next_tier: slots.allowance.cap_on_next_tier,
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
    // `order` and the allowance belong to the same snapshot as the id.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  const close = () => {
    if (saving) return;
    onClose();
  };

  const confirm = async () => {
    if (!workspace) return;
    setSaving(true);
    setError(null);
    try {
      const next = await saveWorkspaceSelection(order, slots.version);
      toast({ title: t("resume.resumed", { name: workspace.name }) });
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
      const details = slotsFullDetails(e);
      if (details) {
        setPreview(null);
        setFull(details);
        return;
      }
      setError(errorMessage(e, t("resume.failed")));
    }
  };

  const suspendInstead = preview?.changes.suspend ?? [];
  const checking = preview === null && error === null && full === null;

  return (
    <AlertDialog open={workspace !== null} onOpenChange={(open) => !open && close()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {t("resume.title", { name: workspace?.name ?? "" })}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2">
              {checking && (
                <>
                  <p>{t("resume.checking")}</p>
                  <LoadingState lines={2} />
                </>
              )}
              {preview && suspendInstead.length === 0 && (
                <p>{t("resume.freeSlot", { name: workspace?.name ?? "" })}</p>
              )}
              {preview && suspendInstead.length > 0 && (
                <>
                  <p>{t("resume.swap", { name: workspace?.name ?? "" })}</p>
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
