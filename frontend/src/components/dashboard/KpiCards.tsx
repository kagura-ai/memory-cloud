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
}

export function KpiCards({
  totalMemories,
  contextCount,
  contextStats,
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
      <KpiCard icon={Brain} label={t("totalMemories")} value={totalMemories} />
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
