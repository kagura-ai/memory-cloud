"use client";

/**
 * Plan Badge Component
 *
 * Issue #149: Plan tier enforcement
 * Issue #350: Customizable display names (S/M/L default)
 *
 * Displays workspace plan tier with color coding. The label is resolved by
 * `planLabelFromEnv` — OSS default S/M/L, with an optional per-locale override
 * for deployments (see lib/utils/planLabel.ts).
 */

import { Badge } from "@/components/ui/badge";
import { useLocale } from "@/i18n";
import { planLabelFromEnv, type PlanTier } from "@/lib/utils/planLabel";
import { cn } from "@/styles/design-tokens";

export type { PlanTier };

interface PlanBadgeProps {
  planName: PlanTier;
  size?: "sm" | "md" | "lg";
  className?: string;
}

const PLAN_COLORS: Record<PlanTier, string> = {
  free: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-100",
  basic: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-100",
  pro: "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-100",
};

const SIZE_CLASSES = {
  sm: "text-xs px-2 py-0.5",
  md: "text-sm px-2.5 py-1",
  lg: "text-base px-3 py-1.5",
};

export function PlanBadge({
  planName,
  size = "md",
  className,
}: PlanBadgeProps) {
  const { locale } = useLocale();
  return (
    <Badge
      className={cn(
        PLAN_COLORS[planName],
        SIZE_CLASSES[size],
        "font-semibold",
        className,
      )}
    >
      {planLabelFromEnv(planName, locale)}
    </Badge>
  );
}
