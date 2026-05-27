"use client";

/**
 * Spend Cap Edit Dialog (Issue #712, #709 follow-up)
 *
 * Per-workspace BYOK embedding spend cap override editor. Clones the Edit
 * Addons Dialog pattern (page.tsx) but extracted as a standalone component so
 * the null/0/positive input mapping can be unit-tested in isolation.
 *
 * Input semantics (admin.ts:204-215):
 * - empty  -> null payload  (clears the override, inherits the tier default)
 * - 0      -> admin lockout (disables all embedding for the workspace)
 * - >0     -> override (backend rejects values above the tier default, HTTP 400)
 */

import { useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { useToast } from "@/hooks/use-toast";
import { updateWorkspaceSpendCap, type SpendCapValues } from "@/lib/api/admin";
import { AlertTriangle, CheckCircle } from "lucide-react";

interface SpendCapEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaceId: string;
  workspaceName: string;
  spendCap: SpendCapValues;
  onSaved: () => void;
}

/** Empty string maps to ``null`` (clear override); any other value to a number. */
const parseInput = (value: string): number | null =>
  value === "" ? null : Number(value);

// Surface the localized warning once usage crosses this fraction of the
// effective cap (mirrors the legacy QuotaWarning 80% threshold, but with
// spend-appropriate, internationalized copy).
const USAGE_WARNING_THRESHOLD = 80;

export function SpendCapEditDialog({
  open,
  onOpenChange,
  workspaceId,
  workspaceName,
  spendCap,
  onSaved,
}: SpendCapEditDialogProps) {
  const t = useTranslations("admin.plans");
  const tCommon = useTranslations("admin.common");
  const locale = useLocale();
  const { toast } = useToast();

  const currencyFormatter = useMemo(
    () => new Intl.NumberFormat(locale, { style: "currency", currency: "USD" }),
    [locale],
  );
  const formatUsd = (value: number): string => currencyFormatter.format(value);

  const [dailyInput, setDailyInput] = useState("");
  const [monthlyInput, setMonthlyInput] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Prefill from the OVERRIDE values (not effective) each time the dialog
  // opens, so "no override" renders as an empty input (placeholder shows the
  // inherited tier default) and an explicit 0 renders as "0", not blank.
  // Deps are the primitive override values (not the spendCap object) so a new
  // object identity with identical values cannot clobber an in-progress edit.
  useEffect(() => {
    if (!open) return;
    setDailyInput(
      spendCap.override_daily_usd === null
        ? ""
        : String(spendCap.override_daily_usd),
    );
    setMonthlyInput(
      spendCap.override_monthly_usd === null
        ? ""
        : String(spendCap.override_monthly_usd),
    );
    setSaveError(null);
  }, [open, spendCap.override_daily_usd, spendCap.override_monthly_usd]);

  const dailyValue = parseInput(dailyInput);
  const monthlyValue = parseInput(monthlyInput);
  // An explicit 0 is a valid-but-destructive lockout; warn loudly but still
  // allow saving (issue #712 defines 0 as an intentional admin action).
  const showLockoutWarning = dailyValue === 0 || monthlyValue === 0;

  // Client-side validation: an empty field is valid (clears the override),
  // but a non-numeric value (which JSON-serializes NaN to null and would
  // silently clear the override) or a negative cap must be blocked before it
  // reaches the backend. Surfaced as an inline field message (frontend.md).
  const fieldError = (value: string): string | null => {
    if (value === "") return null;
    const n = Number(value);
    if (!Number.isFinite(n)) return t("spendCapDialog.invalidNumber");
    if (n < 0) return t("spendCapDialog.negativeNotAllowed");
    return null;
  };
  const dailyError = fieldError(dailyInput);
  const monthlyError = fieldError(monthlyInput);
  const hasFieldError = dailyError !== null || monthlyError !== null;

  const fields = [
    {
      key: "daily" as const,
      label: t("spendCapDialog.dailyLabel"),
      input: dailyInput,
      setInput: setDailyInput,
      tierDefault: spendCap.tier_default_daily_usd,
      effective: spendCap.effective_daily_usd,
      current: spendCap.current_daily_usd,
      warningLabel: t("spendCapDialog.dailyWarningLabel"),
      error: dailyError,
    },
    {
      key: "monthly" as const,
      label: t("spendCapDialog.monthlyLabel"),
      input: monthlyInput,
      setInput: setMonthlyInput,
      tierDefault: spendCap.tier_default_monthly_usd,
      effective: spendCap.effective_monthly_usd,
      current: spendCap.current_monthly_usd,
      warningLabel: t("spendCapDialog.monthlyWarningLabel"),
      error: monthlyError,
    },
  ];

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await updateWorkspaceSpendCap(workspaceId, {
        embedding_daily_cap_usd: dailyValue,
        embedding_monthly_cap_usd: monthlyValue,
      });
      toast({
        title: tCommon("success"),
        description: t("messages.spendCapUpdateSuccess"),
      });
      onOpenChange(false);
      onSaved();
    } catch (err: unknown) {
      // Tier-ceiling rejection (400) and any other save failure surface as an
      // Alert inside the dialog — a toast behind the modal is easy to miss
      // (frontend.md error-surface rule). The dialog stays open.
      setSaveError(
        err instanceof Error ? err.message : t("messages.spendCapUpdateError"),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("spendCapDialog.title")}</DialogTitle>
          <DialogDescription>
            {t("spendCapDialog.description")} <strong>{workspaceName}</strong>
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-4">
          <p className="text-xs text-muted-foreground">
            {t("spendCapDialog.clearHint")}
          </p>

          {fields.map((field) => {
            const limit = field.effective ?? 0;
            const usagePct = limit > 0 ? (field.current / limit) * 100 : null;
            return (
              <div key={field.key} className="space-y-1">
                <Label htmlFor={`spend-cap-${field.key}`}>{field.label}</Label>
                <Input
                  id={`spend-cap-${field.key}`}
                  type="number"
                  min={0}
                  step={0.01}
                  // Omit max when the tier has no ceiling (null) so the input
                  // is unbounded; the backend still rejects values above the
                  // tier default for tiers that have one.
                  max={field.tierDefault ?? undefined}
                  value={field.input}
                  placeholder={
                    field.tierDefault === null
                      ? t("spendCapDialog.noLimit")
                      : formatUsd(field.tierDefault)
                  }
                  onChange={(e) => field.setInput(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">
                  {t("spendCapDialog.currentSpend")}: {formatUsd(field.current)}
                  {field.tierDefault !== null && (
                    <>
                      {" · "}
                      {t("spendCapDialog.tierDefaultHint", {
                        value: formatUsd(field.tierDefault),
                      })}
                    </>
                  )}
                </p>
                {field.error && (
                  <p className="text-xs text-destructive">{field.error}</p>
                )}
                {usagePct !== null && usagePct >= USAGE_WARNING_THRESHOLD && (
                  <Alert>
                    <AlertTriangle className="h-4 w-4" />
                    <AlertDescription>
                      {t("spendCapDialog.usageWarning", {
                        label: field.warningLabel,
                        current: formatUsd(field.current),
                        limit: formatUsd(limit),
                        percentage: usagePct.toFixed(0),
                      })}
                    </AlertDescription>
                  </Alert>
                )}
              </div>
            );
          })}

          {showLockoutWarning && (
            <Alert>
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>
                {t("spendCapDialog.lockoutWarning.title")}
              </AlertTitle>
              <AlertDescription>
                {t("spendCapDialog.lockoutWarning.body")}
              </AlertDescription>
            </Alert>
          )}

          {saveError && (
            <Alert variant="destructive">
              <AlertTitle>{tCommon("error")}</AlertTitle>
              <AlertDescription>{saveError}</AlertDescription>
            </Alert>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {tCommon("cancel")}
          </Button>
          <Button onClick={handleSave} disabled={saving || hasFieldError}>
            <CheckCircle className="h-4 w-4 mr-2" />
            {t("spendCapDialog.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
