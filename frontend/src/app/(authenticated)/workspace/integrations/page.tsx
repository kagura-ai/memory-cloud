"use client";

import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { useWorkspace } from "@/contexts/WorkspaceContext";

const credentials = "/workspace/integrations/credentials";
const tasks = [
  { key: "client", href: credentials + "?tab=api-keys#mcp-connection" },
  { key: "script", href: credentials + "?tab=api-keys#credential-tools" },
  { key: "import", href: "/workspace/resources" },
  { key: "connector", href: "/workspace/integrations/connectors" },
] as const;
const management = [
  { key: "ownApp", href: credentials + "?tab=oauth-apps#credential-tools" },
  { key: "connected", href: credentials + "?tab=api-keys#connected-apps" },
  { key: "provider", href: "/workspace/integrations/external-keys" },
  { key: "secrets", href: "/workspace/secrets" },
] as const;

export default function IntegrationsPage() {
  const t = useTranslations("integrationEntry");
  const { currentWorkspace } = useWorkspace();
  const features = useSystemFeatures();
  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("intro")} />
      {currentWorkspace && <p className="mb-4 text-sm text-muted-foreground">{t("workspace", { name: currentWorkspace.name })}</p>}
      {([["start", tasks], ["manage", management]] as const).map(([group, choices]) => (
        <section key={group} aria-labelledby={group} className="mb-6 space-y-3">
          <h2 id={group} className="text-lg font-semibold">{t(group)}</h2>
          <ul className="grid gap-3 sm:grid-cols-2">
            {choices.filter(({ key }) => key !== "provider" || features?.byok === true).map(({ key, href }) => (
              <li key={key} className="rounded-lg border p-4">
                <Link href={href} aria-describedby={key + "-help"}
                  className="inline-flex min-h-11 items-center gap-2 font-medium text-primary underline underline-offset-4 focus-visible:outline">
                  {t(`tasks.${key}.title`)}<ArrowRight aria-hidden="true" className="h-4 w-4 shrink-0" />
                </Link>
                <p id={key + "-help"} className="text-sm text-muted-foreground">{t(`tasks.${key}.description`)}</p>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </PageContainer>
  );
}
