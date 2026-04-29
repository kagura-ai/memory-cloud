/**
 * Resource Stats Strip
 *
 * Always-visible header metrics for the Resource Detail page.
 * Sits between PageHeader and the Tabs block.
 *
 * Issue #47
 */

"use client";

import { Key, Database, FileJson, Clock } from "lucide-react";
import { useTranslations, useLocale } from "next-intl";
import { KpiCard } from "@/components/ui/kpi-card";
import { formatDateTime, formatRelativeTime } from "@/lib/utils/datetime";
import { useAuth } from "@/contexts/AuthContext";
import type { ResourceListItem } from "@/lib/api/resources";

interface ResourceStatsStripProps {
  resource: ResourceListItem;
}

export function ResourceStatsStrip({ resource }: ResourceStatsStripProps) {
  const t = useTranslations("resources");
  const locale = useLocale();
  const { user } = useAuth();
  const timezone = user?.timezone || "UTC";

  // Route number formatting through the app's next-intl locale so grouping /
  // decimal rules match the UI (KpiCard's default toLocaleString() picks the
  // runtime default locale, which can diverge from the operator's selection).
  const numberFormatter = new Intl.NumberFormat(locale);

  // Visual hierarchy: Memories is the headline KPI (the size of the resource
  // is what an operator actually watches), Last Activity is a secondary
  // signal, and tokens / schema_version are supporting context. Cards that
  // report a benign null state (no schema yet, no events yet) take the
  // ``muted`` tone so they recede instead of competing with the headline.
  //
  // The "Manage tokens" deep-link that used to overlay the Tokens KpiCard
  // was removed in #326 polish — the Tokens tab is now reachable directly
  // from the in-page tab bar, so the deep-link was redundant and
  // inconsistent with how the other KpiCards behave.
  const hasSchema = resource.current_schema_version !== null;

  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
      <KpiCard
        icon={Database}
        label={t("stats.memories")}
        value={numberFormatter.format(resource.memory_count)}
        tone={resource.memory_count > 0 ? "primary" : "muted"}
      />

      <KpiCard
        icon={Clock}
        label={t("stats.lastActivity")}
        value={formatRelativeTime(resource.updated_at, locale)}
        valueTitle={formatDateTime(resource.updated_at, timezone, locale)}
        tone="secondary"
      />

      <KpiCard
        icon={Key}
        label={t("stats.activeTokens")}
        value={numberFormatter.format(resource.token_count)}
        tone={resource.token_count > 0 ? "secondary" : "muted"}
      />

      <KpiCard
        icon={FileJson}
        label={t("stats.schemaVersion")}
        value={hasSchema ? `v${resource.current_schema_version}` : "—"}
        subtext={hasSchema ? undefined : t("stats.noSchemaYet")}
        tone={hasSchema ? "secondary" : "muted"}
      />
    </div>
  );
}
