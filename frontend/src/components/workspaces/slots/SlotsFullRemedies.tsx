"use client";

/**
 * What to do when no active slot can be freed for a suspended workspace
 * (#2919): every active slot is held by a workspace with a paid subscription
 * (409 `SLOT-002`, or a preview that leaves the workspace `unmet`).
 */

import { useTranslations } from "next-intl";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { useLocale } from "@/i18n";
import { planLabelForTier } from "@/lib/utils/planLabel";

export function SlotsFullRemedies({
  nextTier,
  capOnNextTier,
}: {
  nextTier: string | null | undefined;
  capOnNextTier: number | null | undefined;
}) {
  const t = useTranslations("workspaceSlots");
  const { locale } = useLocale();
  const canUpgrade =
    typeof nextTier === "string" && typeof capOnNextTier === "number";
  return (
    <Alert variant="warning">
      <AlertTitle>{t("full.title")}</AlertTitle>
      <AlertDescription className="space-y-1">
        <p>{t("full.description")}</p>
        {canUpgrade && (
          <p>
            {t("full.upgrade", {
              plan: planLabelForTier(nextTier, undefined, locale),
              cap: capOnNextTier,
            })}
          </p>
        )}
        <p>{t("full.delete")}</p>
      </AlertDescription>
    </Alert>
  );
}
