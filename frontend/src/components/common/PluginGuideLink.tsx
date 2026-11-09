"use client";

import { ArrowUpRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useLocale } from "@/i18n";

/** Shared stable destination for onboarding and connection settings (#2736). */
export function getPluginGuideUrl(locale: string): string {
  return `https://www.kagura-ai.com${locale === "ja" ? "/ja" : ""}/help/guide/#plugins`;
}

export function PluginGuideLink() {
  const { locale } = useLocale();
  const t = useTranslations("pluginGuide");
  return (
    <div className="space-y-1">
      <a
        href={getPluginGuideUrl(locale)}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex min-h-11 items-center gap-1 text-sm font-medium text-primary underline underline-offset-4 hover:text-primary/80"
      >
        {t("label")}
        <ArrowUpRight aria-hidden="true" className="h-4 w-4" />
        <span className="sr-only">{t("newTab")}</span>
      </a>
      <p className="text-xs leading-5 text-muted-foreground">{t("description")}</p>
    </div>
  );
}
