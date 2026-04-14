/**
 * Resource Data tab placeholder.
 *
 * The Data browser (paginated ingested records + JSONB viewer) is tracked
 * in the follow-up issue #316 for v0.13.0. This placeholder keeps the tab
 * URL shape stable while the feature is pending.
 *
 * Issue #47 (placeholder) · Issue #316 (implementation)
 */

"use client";

import { Database, ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";
import { EmptyState } from "@/components/ui/empty-state";

export function ResourceDataTabPlaceholder() {
  const t = useTranslations("resources.data");

  return (
    <EmptyState
      icon={Database}
      title={t("comingSoonTitle")}
      description={t("comingSoonDescription")}
    >
      <a
        href="https://github.com/kagura-ai/memory-cloud/issues/316"
        target="_blank"
        rel="noopener noreferrer"
        className="mt-4 inline-flex items-center gap-1.5 text-sm text-brand-green-600 hover:text-brand-green-700 dark:text-brand-green-400"
      >
        {t("trackProgress")} <ExternalLink className="h-3.5 w-3.5" />
      </a>
    </EmptyState>
  );
}
