"use client";

import { useTranslations } from "next-intl";
import { Badge } from "@/components/ui/badge";

export function CurrentContextBadge() {
  const t = useTranslations("contexts");
  return (
    <Badge
      variant="default"
      className="text-[10px] px-1.5 py-0 ring-2 ring-brand-green-300 dark:ring-brand-green-700"
      aria-label={t("current")}
    >
      {t("current")}
    </Badge>
  );
}
