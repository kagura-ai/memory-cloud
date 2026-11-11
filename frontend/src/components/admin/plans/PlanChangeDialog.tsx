"use client";

/**
 * Change Tier dialog (Issue #149, #1548, #1848; one dialog since #2765).
 *
 * The Quotas page and the admin user detail each had a plan change dialog;
 * #2765 keeps one. It sends what the Quotas page sent —
 * `updateWorkspacePlan(workspaceId, { plan_name })` — shows the Quotas page's
 * warning only once a different plan is chosen, and keeps confirm disabled
 * until then. From the user detail's dialog it keeps the `free` fallback for
 * a workspace without a plan and the server's message in the failure toast.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { CheckCircle } from "lucide-react";
import { useLocale } from "@/i18n";
import { PlanBadge } from "@/components/common/PlanBadge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { updateWorkspacePlan } from "@/lib/api/admin";
import {
  PLAN_TIER_ORDER,
  planLabelFromEnv,
  type PlanTier,
} from "@/lib/utils/planLabel";

export interface PlanChangeTarget {
  id: string;
  name: string;
  /** The workspace's current plan; empty or missing reads as `free`. */
  planName?: string | null;
}

interface PlanChangeDialogProps {
  workspace: PlanChangeTarget;
  onClose: () => void;
  /** Called after the plan changed, so the caller reloads what shows it. */
  onChanged: () => void;
}

export function PlanChangeDialog({
  workspace,
  onClose,
  onChanged,
}: PlanChangeDialogProps) {
  const t = useTranslations("admin.plans");
  const tCommon = useTranslations("admin.common");
  // The plan-label locale comes from the same hook PlanBadge reads (#1848).
  const { locale } = useLocale();
  const { toast } = useToast();
  const currentPlan = workspace.planName || "free";
  const [newPlan, setNewPlan] = useState<string>(currentPlan);
  const [submitting, setSubmitting] = useState(false);
  const unchanged = newPlan === currentPlan;

  const handleConfirm = async () => {
    if (unchanged) return;
    setSubmitting(true);
    try {
      await updateWorkspacePlan(workspace.id, {
        plan_name: newPlan as PlanTier,
      });
      toast({
        title: tCommon("success"),
        description: t("messages.changeSuccess", {
          oldPlan: currentPlan.toUpperCase(),
          newPlan: newPlan.toUpperCase(),
        }),
      });
      onClose();
      onChanged();
    } catch (err: unknown) {
      toast({
        title: tCommon("error"),
        description:
          err instanceof Error ? err.message : t("messages.changeError"),
        variant: "destructive",
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("changePlanDialog.title")}</DialogTitle>
          <DialogDescription>
            {t("changePlanDialog.description")}{" "}
            <strong>{workspace.name}</strong>
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <div>
            <p className="text-sm font-medium">
              {t("changePlanDialog.currentPlan")}
            </p>
            <div className="mt-2">
              <PlanBadge planName={currentPlan as PlanTier} />
            </div>
          </div>

          <div>
            <p className="text-sm font-medium">
              {t("changePlanDialog.newPlan")}
            </p>
            <Select value={newPlan} onValueChange={setNewPlan}>
              <SelectTrigger
                className="mt-2"
                aria-label={t("changePlanDialog.newPlan")}
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PLAN_TIER_ORDER.map((plan) => (
                  <SelectItem key={plan} value={plan}>
                    {/* Same resolver as the PlanBadge beside it (#1848). */}
                    {planLabelFromEnv(plan, locale)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {!unchanged && (
            <div className="rounded-md bg-yellow-50 p-3 dark:bg-yellow-950">
              <p className="text-sm text-yellow-800 dark:text-yellow-100">
                {t("changePlanDialog.warning")}
              </p>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t("changePlanDialog.cancel")}
          </Button>
          <Button onClick={handleConfirm} disabled={unchanged || submitting}>
            <CheckCircle className="mr-2 h-4 w-4" />
            {t("changePlanDialog.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
