"use client";

/**
 * #1939: paid-only features paused on a plan with no allowance for them
 * (typically a workspace back on Free).
 *
 * Nothing is deleted on a downgrade — connectors, resource tokens, Sleep
 * settings and public contexts are all kept — but the work they drive stops
 * until the workspace re-subscribes. The list comes from the plan API
 * (`WorkspacePlanInfo.suspended`), which only names a feature the workspace
 * still has something for, so a Free workspace that never used them sees no
 * banner.
 */

import Link from "next/link";
import { useTranslations } from "next-intl";
import { PauseCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

/**
 * Paid-only features the plan API may report as paused. Mirrors
 * `SUSPENDABLE_FEATURES` in `backend/src/services/plan_suspension.py`.
 */
export const SUSPENDABLE_FEATURES = [
  "connectors",
  "resources",
  "sleep",
  "public",
] as const;
export type SuspendableFeature = (typeof SUSPENDABLE_FEATURES)[number];

const isSuspendable = (name: string): name is SuspendableFeature =>
  (SUSPENDABLE_FEATURES as readonly string[]).includes(name);

interface SuspendedFeaturesBannerProps {
  /** Wire names from the plan API; unknown names are ignored. */
  suspended: readonly string[];
  /** Add a link to the plan page (for surfaces other than the plan page). */
  showPlanLink?: boolean;
  className?: string;
}

export function SuspendedFeaturesBanner({
  suspended,
  showPlanLink = false,
  className,
}: SuspendedFeaturesBannerProps) {
  const t = useTranslations("workspace");
  const features = suspended.filter(isSuspendable);
  if (features.length === 0) return null;

  return (
    <Alert variant="warning" className={className}>
      <PauseCircle className="h-4 w-4" />
      <AlertTitle>{t("planPage.suspended.title")}</AlertTitle>
      <AlertDescription>
        <p>{t("planPage.suspended.description")}</p>
        <ul className="my-1 list-disc pl-5">
          {features.map((feature) => (
            <li key={feature}>{t(`planPage.suspended.features.${feature}`)}</li>
          ))}
        </ul>
        <p>
          {t("planPage.suspended.resume")}
          {showPlanLink && (
            <>
              {" "}
              <Link
                href="/workspace/settings/plan"
                className="font-medium underline underline-offset-2"
              >
                {t("planPage.suspended.viewPlan")}
              </Link>
            </>
          )}
        </p>
      </AlertDescription>
    </Alert>
  );
}
