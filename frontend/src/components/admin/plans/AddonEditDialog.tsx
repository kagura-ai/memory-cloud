"use client";

/**
 * Edit Addons dialog (Issue #325, #663, #800, #1561).
 *
 * Moved out of the Quotas page (`/admin/plans`) in #2765; the admin user
 * detail opens it for a workspace the user owns. Sends what the Quotas page
 * sent: `updateWorkspaceAddons(workspaceId, buildUpdateAddonRequest(values))`
 * with every addon field as an absolute value.
 *
 * The form is seeded from `quotaDetail` when the dialog mounts; the parent
 * mounts it while the dialog is open.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { CheckCircle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import {
  updateWorkspaceAddons,
  type WorkspaceQuotaDetail,
} from "@/lib/api/admin";
import {
  ADDON_TYPES,
  buildUpdateAddonRequest,
  computeAddonEffectivePreview,
  findInvalidAddonValues,
  snapshotAddonValues,
  type AddonKey,
  type AddonValuesByKey,
} from "./_addon-types";

interface AddonEditDialogProps {
  quotaDetail: WorkspaceQuotaDetail;
  onClose: () => void;
  /** Called after a successful save, so the caller refreshes its quota view. */
  onSaved: () => void;
}

export function AddonEditDialog({
  quotaDetail,
  onClose,
  onSaved,
}: AddonEditDialogProps) {
  const t = useTranslations("admin.plans");
  const tCommon = useTranslations("admin.common");
  const { toast } = useToast();
  // Issue #663: every addon dimension in one record so ``ADDON_TYPES`` drives
  // both the inputs and the PUT body.
  const [addonValues, setAddonValues] = useState<AddonValuesByKey>(() =>
    snapshotAddonValues(quotaDetail),
  );
  // Issue #1561: structured warning codes returned by the addon PUT (e.g.
  // `connectors_feature_missing`). Non-empty keeps the dialog open after a
  // successful save so the admin reads why the grant is inert on this tier.
  const [addonWarnings, setAddonWarnings] = useState<string[]>([]);
  const setAddonValue = (key: AddonKey, value: number): void => {
    setAddonValues((prev) => ({ ...prev, [key]: value }));
  };
  // #800: addon keys whose current form value can't be saved (non-multiple of
  // perUnit, or negative), derived from the live form state so the warning
  // and the Save gate stay in lock-step.
  const invalidAddons = findInvalidAddonValues(addonValues);

  const handleSave = async () => {
    try {
      const result = await updateWorkspaceAddons(
        quotaDetail.workspace_id,
        buildUpdateAddonRequest(addonValues),
      );
      toast({
        title: tCommon("success"),
        description: t("messages.addonUpdateSuccess"),
      });
      // Issue #1561: the save succeeded either way; a non-empty `warnings`
      // means a grant is inert on this tier, so keep the dialog open.
      const warnings = result?.warnings ?? [];
      setAddonWarnings(warnings);
      onSaved();
      if (warnings.length === 0) {
        onClose();
      }
    } catch (err: unknown) {
      toast({
        title: tCommon("error"),
        description:
          err instanceof Error ? err.message : t("messages.addonUpdateError"),
        variant: "destructive",
      });
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("addonDialog.title")}</DialogTitle>
          <DialogDescription>
            {t("addonDialog.description")}{" "}
            <strong>{quotaDetail.workspace_name}</strong>
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          {/* Issue #1561: the grant was stored but is inert on this tier.
              Informational, not an error → Alert, not destructive. Unknown
              codes fall back to a generic line. */}
          {addonWarnings.length > 0 && (
            <Alert>
              <AlertTitle>{t("addonDialog.featureWarning.title")}</AlertTitle>
              <AlertDescription className="space-y-1">
                {addonWarnings.map((code) => (
                  <p key={code}>
                    {code === "connectors_feature_missing"
                      ? t("addonDialog.featureWarning.connectorsFeatureMissing")
                      : code === "operation_history_feature_missing"
                        ? t(
                            "addonDialog.featureWarning.operationHistoryFeatureMissing",
                          )
                        : t("addonDialog.featureWarning.generic", { code })}
                  </p>
                ))}
              </AlertDescription>
            </Alert>
          )}

          {/* Issue #800: a legacy cache can hold a value that is not a
              multiple of perUnit, which the backend rejects with 400. Name
              the offending addons and disable Save until they are fixed. */}
          {invalidAddons.length > 0 && (
            <Alert>
              <AlertTitle>{t("addonDialog.invalidWarning.title")}</AlertTitle>
              <AlertDescription>
                {t("addonDialog.invalidWarning.body", {
                  keys: invalidAddons
                    .map((key) => t(`addonDialog.${key}`))
                    .join(", "),
                })}
              </AlertDescription>
            </Alert>
          )}

          {/* Issue #663: inputs driven by ADDON_TYPES. The "effective"
              preview reflects the backend's _zero_floor clamp. */}
          {ADDON_TYPES.map((meta) => {
            const base = quotaDetail.base[meta.baseField] ?? 0;
            const state = addonValues[meta.key];
            const usage = meta.usageField
              ? (quotaDetail.usage[meta.usageField] ?? 0)
              : null;
            const effective = computeAddonEffectivePreview(meta, base, state);
            const perUnitHint = meta.unitSuffix
              ? t("addonDialog.perUnitWithSuffix", {
                  count: meta.perUnit,
                  unit: meta.unitSuffix,
                })
              : t("addonDialog.perUnit", { count: meta.perUnit });
            return (
              <div key={meta.key}>
                <Label htmlFor={`addon-${meta.key}`}>
                  {t(`addonDialog.${meta.key}`)}{" "}
                  <span className="text-xs font-normal text-muted-foreground">
                    {perUnitHint}
                  </span>
                  {meta.proOnly && (
                    <span className="ml-2 text-xs font-normal text-muted-foreground">
                      {t("addonDialog.proOnlyInline")}
                    </span>
                  )}
                </Label>
                <div className="mt-1 flex items-center gap-2">
                  <Input
                    id={`addon-${meta.key}`}
                    type="number"
                    min={0}
                    step={meta.perUnit}
                    value={state}
                    onChange={(e) =>
                      setAddonValue(meta.key, Number(e.target.value))
                    }
                  />
                  <span className="whitespace-nowrap text-xs text-muted-foreground">
                    {t("addonDialog.effective")}: {effective.toLocaleString()}
                    {meta.unitSuffix ? ` ${meta.unitSuffix}` : ""}
                    {usage !== null && (
                      <>
                        {" "}
                        ({usage} {t("addonDialog.used")})
                      </>
                    )}
                  </span>
                </div>
              </div>
            );
          })}

          {/* #663: only addons with a live usage counter can be reduced
              "below current usage" (the backend's LD-7 guard, HTTP 400). */}
          {ADDON_TYPES.some(
            (meta) =>
              meta.usageField !== undefined &&
              addonValues[meta.key] < quotaDetail.addon[meta.addonField],
          ) && (
            <div className="rounded-md bg-yellow-50 p-3 dark:bg-yellow-950">
              <p className="text-sm text-yellow-800 dark:text-yellow-100">
                {t("addonDialog.reductionWarning", {
                  memories: quotaDetail.usage.memories,
                  members: quotaDetail.usage.members,
                  contexts: quotaDetail.usage.contexts,
                })}
              </p>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {tCommon("cancel")}
          </Button>
          {/* #800: the PUT sends every field, so one invalid value would 400
              the whole request. */}
          <Button onClick={handleSave} disabled={invalidAddons.length > 0}>
            <CheckCircle className="mr-2 h-4 w-4" />
            {t("addonDialog.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
