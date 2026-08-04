"use client";

import { useMemo } from "react";
import { useTranslations } from "next-intl";
import { Brain, Layers, Zap, Users } from "lucide-react";
import { KpiCard } from "@/components/ui/kpi-card";
import type { ContextStatsResponse } from "@/lib/api/workspaces";

interface KpiCardsProps {
  totalMemories: number;
  contextCount: number;
  contextStats: ContextStatsResponse | null;
  /**
   * #1496: how many of `totalMemories` cannot be found by recall.
   *
   * The count itself is not wrong — those memories exist and are charged for.
   * It is just not the number the user thinks it is, so the qualification
   * belongs on this card rather than somewhere else on the page.
   */
  unsearchableCount?: number;
}

export function KpiCards({
  totalMemories,
  contextCount,
  contextStats,
  unsearchableCount = 0,
}: KpiCardsProps) {
  const t = useTranslations("dashboard");

  const { apiCallsWeek, activeUsersWeek } = useMemo(() => {
    if (!contextStats) return { apiCallsWeek: 0, activeUsersWeek: 0 };
    return {
      apiCallsWeek: contextStats.contexts.reduce(
        (sum, ctx) => sum + ctx.api_calls_week,
        0,
      ),
      activeUsersWeek: contextStats.contexts.reduce(
        (sum, ctx) => sum + ctx.active_users_week,
        0,
      ),
    };
  }, [contextStats]);

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
      <KpiCard
        icon={Brain}
        label={t("totalMemories")}
        value={totalMemories}
        subtext={
          unsearchableCount > 0
            ? t("unsearchableSubtext", { count: unsearchableCount })
            : undefined
        }
      />
      <KpiCard icon={Layers} label={t("contextCount")} value={contextCount} />
      <KpiCard icon={Zap} label={t("apiCallsWeek")} value={apiCallsWeek} />
      <KpiCard
        icon={Users}
        label={t("activeUsersWeek")}
        value={activeUsersWeek}
      />
    </div>
  );
}
