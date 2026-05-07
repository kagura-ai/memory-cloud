"use client";

import { Brain, BrainCircuit, BrainCog } from "lucide-react";
import { useTranslations } from "next-intl";
import type { SleepMode } from "@/lib/types/context";

interface SleepModeBadgeProps {
  mode: SleepMode;
}

const MODE_CONFIG: Record<
  SleepMode,
  {
    Icon: typeof Brain;
    color: string;
    labelKey:
      | "sleepModeBadgeFull"
      | "sleepModeBadgeEdgesOnly"
      | "sleepModeBadgeSkip";
    descKey:
      | "sleepModeFullDesc"
      | "sleepModeEdgesOnlyDesc"
      | "sleepModeSkipDesc";
  }
> = {
  full: {
    Icon: Brain,
    color: "text-emerald-600 dark:text-emerald-400",
    labelKey: "sleepModeBadgeFull",
    descKey: "sleepModeFullDesc",
  },
  edges_only: {
    Icon: BrainCircuit,
    color: "text-amber-600 dark:text-amber-400",
    labelKey: "sleepModeBadgeEdgesOnly",
    descKey: "sleepModeEdgesOnlyDesc",
  },
  skip: {
    Icon: BrainCog,
    color: "text-gray-400 dark:text-gray-500",
    labelKey: "sleepModeBadgeSkip",
    descKey: "sleepModeSkipDesc",
  },
};

export function SleepModeBadge({ mode }: SleepModeBadgeProps) {
  const t = useTranslations("contexts");
  const tSettings = useTranslations("contextSettings");
  const { Icon, color, labelKey, descKey } = MODE_CONFIG[mode];

  return (
    <span
      className={`inline-flex items-center gap-1 text-xs ${color}`}
      title={tSettings(descKey)}
      aria-label={t(labelKey)}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      {t(labelKey)}
    </span>
  );
}
