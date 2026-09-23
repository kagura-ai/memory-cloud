"use client";

/**
 * Quota Warning Component
 *
 * Issue #149: Plan tier enforcement
 * Issue #1647: every user-facing string comes from the `quotaWarning`
 * namespace, so the dashboard block is no longer English-only.
 * Issue #1643: the caller still decides WHAT the upgrade button does
 * (`onUpgrade`); this component decides WHETHER it exists, via
 * `useCanUpgrade`. Keeping that here means no caller re-derives the rule.
 *
 * Displays warning when approaching or exceeding quota limits.
 */

import { useTranslations } from "next-intl";
import { useCanUpgrade } from "@/hooks/useCanUpgrade";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { AlertCircle, AlertTriangle, XCircle } from "lucide-react";
import { cn } from "@/styles/design-tokens";

interface QuotaWarningProps {
  current: number;
  limit: number;
  /**
   * Display name of the metered resource, ALREADY TRANSLATED by the caller
   * (e.g. `t("memories")`).
   *
   * It is interpolated into the warning sentence as an opaque token — never
   * lowercased, pluralised or otherwise inflected (#1647). Each locale owns the
   * whole sentence, so only the message file decides how the noun reads.
   */
  resourceLabel: string;
  unit?: string;
  onUpgrade?: () => void;
  className?: string;
}

/**
 * Quota warning alert with progress bar.
 *
 * Displays:
 * - Nothing if usage < 80%
 * - Warning (yellow) if 80% <= usage < 95%
 * - Critical (red) if usage >= 95%
 * - Exceeded (destructive) if usage >= 100%
 *
 * @param current - Current usage
 * @param limit - Quota limit
 * @param resourceLabel - Translated resource name (e.g. "Memories", "メモリー")
 * @param unit - Unit label (e.g., "MB", "calls")
 * @param onUpgrade - Callback for upgrade button
 */
export function QuotaWarning({
  current,
  limit,
  resourceLabel,
  unit = "",
  onUpgrade,
  className,
}: QuotaWarningProps) {
  const t = useTranslations("quotaWarning");
  // #1643: above the early return below — a hook may not sit under a
  // conditional return.
  const canUpgrade = useCanUpgrade();
  const percentage = limit > 0 ? (current / limit) * 100 : 0;

  // Don't show warning if below 80%
  if (percentage < 80) {
    return null;
  }

  // Determine severity
  const isExceeded = percentage >= 100;
  const isCritical = percentage >= 95;

  const variant = isExceeded || isCritical ? "destructive" : "default";

  const Icon = isExceeded ? XCircle : isCritical ? AlertTriangle : AlertCircle;

  const title = isExceeded
    ? t("titleExceeded")
    : isCritical
      ? t("titleCritical")
      : t("titleWarning");

  const formatNumber = (num: number) => {
    return num.toLocaleString();
  };

  return (
    <Alert variant={variant} className={cn("mb-4", className)}>
      <Icon className="h-4 w-4" />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <div className="mt-2 space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="font-medium">
              {resourceLabel}: {formatNumber(current)} / {formatNumber(limit)}{" "}
              {unit}
            </span>
            <span className="font-bold">{percentage.toFixed(1)}%</span>
          </div>

          <Progress
            value={Math.min(percentage, 100)}
            className={cn(
              "h-2",
              isExceeded || isCritical
                ? "[&>div]:bg-red-500"
                : "[&>div]:bg-yellow-500",
            )}
          />

          {isExceeded && (
            <p className="text-sm font-medium mt-2">
              {t("bodyExceeded", { resource: resourceLabel })}
            </p>
          )}

          {isCritical && !isExceeded && (
            <p className="text-sm mt-2">
              {t("bodyCritical", { resource: resourceLabel })}
            </p>
          )}

          {/* #1643: the usage numbers, the bar and the "delete some or
              upgrade" sentence above always render; only the button is
              withheld where the Plan page is unreachable. */}
          {onUpgrade &&
            canUpgrade === true &&
            (percentage >= 95 || isExceeded) && (
              <Button
                onClick={onUpgrade}
                size="sm"
                variant={isExceeded ? "destructive" : "default"}
                className="mt-2"
              >
                {t("upgrade")}
              </Button>
            )}
        </div>
      </AlertDescription>
    </Alert>
  );
}
