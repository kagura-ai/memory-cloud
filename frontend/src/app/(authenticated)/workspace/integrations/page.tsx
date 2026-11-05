"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { WorkspacePageHeader as PageHeader } from "@/components/dashboard/WorkspacePageHeader";
import { McpConnectionCard } from "@/components/credentials/McpConnectionCard";
import { useWorkspace } from "@/contexts/WorkspaceContext";

const credentials = "/workspace/integrations/credentials";
const tasks = [
  { key: "connected", section: "connectedApps", href: credentials + "?tab=connected-apps" },
  { key: "script", section: "apiKeys", href: credentials + "?tab=api-keys#credential-tools" },
  { key: "ownApp", section: "oauthApps", href: credentials + "?tab=oauth-apps#credential-tools" },
  { key: "secrets", section: "secrets", href: "/workspace/secrets" },
  { key: "import", section: "resources", href: "/workspace/resources" },
  { key: "connector", section: "connectors", href: "/workspace/integrations/connectors" },
  { key: "provider", section: "providerKeys", href: "/workspace/integrations/external-keys" },
] as const;

export default function IntegrationsPage() {
  const t = useTranslations("integrationEntry");
  const navigation = useTranslations("sectionNavigation");
  const { currentWorkspace } = useWorkspace();
  return (
    <PageContainer>
      <PageHeader title={t("title")} />
      {currentWorkspace && (
        <p className="mb-4 text-sm text-muted-foreground">
          {t("workspace", { name: currentWorkspace.name })}
        </p>
      )}
      <McpConnectionCard variant="summary" />
      <section aria-labelledby="start" className="mb-6 space-y-3">
        <h2 id="start" className="text-lg font-semibold">{t("start")}</h2>
        <ul className="grid gap-3 sm:grid-cols-2">
          {tasks.map(({ key, section, href }) => (
            <li key={key}>
              <Link
                href={href}
                aria-labelledby={key + "-title"}
                aria-describedby={key + "-help"}
                className="group block h-full space-y-3 rounded-lg border p-4 hover:border-blue-300 hover:bg-slate-50 hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2 dark:hover:border-blue-700 dark:hover:bg-slate-800/60 dark:focus-visible:ring-offset-slate-900 motion-safe:transition-all motion-safe:duration-150"
              >
                <span className="inline-flex rounded-md bg-slate-100 px-2 py-1 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                  {navigation(section)}
                </span>
                <h3 id={key + "-title"} className="flex items-center justify-between gap-2 font-medium">
                  {t(`tasks.${key}.title`)}
                  <ArrowRight aria-hidden="true" className="h-4 w-4 shrink-0 text-muted-foreground group-hover:text-blue-600 dark:group-hover:text-blue-300 motion-safe:transition-transform motion-safe:group-hover:translate-x-1" />
                </h3>
                <p id={key + "-help"} className="text-sm text-muted-foreground">
                  {t(`tasks.${key}.description`)}
                </p>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </PageContainer>
  );
}
