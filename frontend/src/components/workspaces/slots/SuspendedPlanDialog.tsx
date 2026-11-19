"use client";

/**
 * Before the billing handoff for a SUSPENDED workspace (#2919): a paid plan
 * resumes it, and when the new plan adds no slot another workspace is
 * suspended instead. Says so, then hands off on Continue. Errors stay inside
 * the dialog.
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
import { InlineSpinner } from "@/components/common/LoadingState";
import { ApiError } from "@/lib/api/base";
import { mintBillingHandoff } from "@/lib/api/billing";
import type { SlotWorkspace } from "@/lib/api/workspaceSlots";

export function SuspendedPlanDialog({
  workspace,
  onClose,
  onCloseAutoFocus,
}: {
  /** The suspended workspace; null = closed. */
  workspace: SlotWorkspace | null;
  onClose: () => void;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  const t = useTranslations("workspaceSlots");
  const tCommon = useTranslations("common");
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = workspace?.name ?? "";

  const close = () => {
    if (opening) return;
    setError(null);
    onClose();
  };

  const proceed = async () => {
    if (!workspace) return;
    setOpening(true);
    setError(null);
    try {
      const res = await mintBillingHandoff(workspace.id);
      if (res.url) {
        window.location.href = res.url;
        return;
      }
      setError(t("planUnavailable"));
    } catch (e) {
      // 503 (BILLING-002): billing is not configured on this deployment.
      setError(
        !(e instanceof ApiError && e.status === 503) &&
          e instanceof Error &&
          e.message
          ? e.message
          : t("planUnavailable"),
      );
    } finally {
      setOpening(false);
    }
  };

  return (
    <AlertDialog open={workspace !== null} onOpenChange={(open) => !open && close()}>
      <AlertDialogContent onCloseAutoFocus={onCloseAutoFocus}>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("suspendedPlan.title", { name })}</AlertDialogTitle>
          <AlertDialogDescription>{t("suspendedPlan.body")}</AlertDialogDescription>
        </AlertDialogHeader>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={opening}>{tCommon("cancel")}</AlertDialogCancel>
          <Button
            type="button"
            onClick={() => void proceed()}
            disabled={opening}
            aria-busy={opening}
          >
            {opening && <InlineSpinner size="sm" className="mr-2" />}
            {t("suspendedPlan.continue")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
