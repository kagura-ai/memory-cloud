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
import Link from "next/link";
import { KpiCard } from "@/components/ui/kpi-card";
import { formatRelativeTime } from "@/lib/utils/datetime";
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

  const tokensHref = `/workspace/integrations/credentials?tab=resource-tokens&resource_id=${encodeURIComponent(
    resource.resource_id,
  )}`;

  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
      <div className="relative">
        <KpiCard
          icon={Key}
          label={t("stats.activeTokens")}
          value={resource.token_count}
        />
        <Link
          href={tokensHref}
          className="absolute right-3 top-3 text-xs text-muted-foreground hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring rounded"
        >
          {t("stats.manage")}
        </Link>
      </div>

      <KpiCard
        icon={Database}
        label={t("stats.memories")}
        value={resource.memory_count}
      />

      <KpiCard
        icon={FileJson}
        label={t("stats.schemaVersion")}
        value={
          resource.current_schema_version !== null
            ? `v${resource.current_schema_version}`
            : "—"
        }
        subtext={
          resource.current_schema_version === null
            ? t("stats.noSchemaYet")
            : undefined
        }
      />

      <KpiCard
        icon={Clock}
        label={t("stats.lastActivity")}
        value={formatRelativeTime(resource.updated_at, timezone, locale)}
      />
    </div>
  );
}
