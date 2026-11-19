"use client";

/**
 * Delete an owned workspace from Workspace management (#2919), with the
 * wording of the workspace settings page. A running paid subscription still
 * blocks the deletion (#1940, 409 `BILLING-005`): said inside the dialog.
 */

import { useState } from "react";
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
import { useToast } from "@/hooks/use-toast";
import { deleteWorkspace } from "@/lib/api/workspaces";
import type { SlotWorkspace } from "@/lib/api/workspaceSlots";
import { BILLING_CONTRACT_ACTIVE, errorCode, errorMessage } from "./slotErrors";

export function DeleteWorkspaceDialog({
  workspace,
  onClose,
  onDeleted,
}: {
  /** The workspace to delete; null = closed. */
  workspace: SlotWorkspace | null;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const t = useTranslations("workspace");
  const tSlots = useTranslations("workspaceSlots");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    if (deleting) return;
    setError(null);
    onClose();
  };

  const confirm = async () => {
    if (!workspace) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteWorkspace(workspace.id);
      toast({ title: t("workspaceDeleted") });
      setDeleting(false);
      onDeleted();
    } catch (e) {
      setError(
        errorCode(e) === BILLING_CONTRACT_ACTIVE
          ? t("deleteBlockedBySubscription")
          : errorMessage(e, tSlots("deleteDialog.failed")),
      );
      setDeleting(false);
    }
  };

  return (
    <AlertDialog open={workspace !== null} onOpenChange={(open) => !open && close()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="text-red-600 dark:text-red-400">
            {t("deleteConfirmTitle")}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2">
              <p>
                {t("deleteWorkspaceDialogDesc", {
                  workspaceName: workspace?.name ?? "",
                })}
              </p>
              <ul className="list-inside list-disc space-y-1 text-sm">
                <li>{t("allContexts")}</li>
                <li>{t("allMemories")}</li>
                <li>{t("allMembers")}</li>
              </ul>
              <p className="font-semibold text-red-600 dark:text-red-400">
                {tCommon("thisActionCannotBeUndone")}
              </p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>{tCommon("cancel")}</AlertDialogCancel>
          <Button
            type="button"
            onClick={() => void confirm()}
            disabled={deleting}
            className="bg-red-600 text-white hover:bg-red-700"
          >
            {deleting ? t("deleting") : t("deleteWorkspace")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
