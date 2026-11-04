"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { FeatureGuide } from "@/components/common/FeatureGuide";
import {
  CategoryTabs,
  CategoryTabsList,
  CategoryTabsTrigger,
  CategoryTabsContent,
} from "@/components/ui/tabs";
import { useTabParam } from "@/hooks/useTabParam";
import { APIKeysTabPanel } from "@/components/credentials/APIKeysTabPanel";
import { McpConnectionCard } from "@/components/credentials/McpConnectionCard";
import { ConnectedAppsSection } from "@/components/credentials/ConnectedAppsSection";
import { OAuthAppsTabPanel } from "@/components/credentials/OAuthAppsTabPanel";

// "oauth-apps" is kept as the URL value for existing links even though the
// tab now holds custom apps only (#1836).
const CREDENTIAL_TABS = ["api-keys", "oauth-apps"] as const;

export default function CredentialsPage() {
  const t = useTranslations("credentials");
  const entry = useTranslations("integrationEntry");
  const [tab, setTab] = useTabParam("api-keys", "tab", CREDENTIAL_TABS);

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      <Link href="/workspace/integrations" className="inline-block py-2 text-sm text-primary underline">{entry("back")}</Link>
      <FeatureGuide storageKey="credentials" title={t("featureGuide.title")}>
        <p>{t("featureGuide.overview")}</p>
        <p>{t("featureGuide.choosingMethod")}</p>
      </FeatureGuide>

      <div id="mcp-connection" tabIndex={-1}><McpConnectionCard /></div>

      <div id="credential-tools" tabIndex={-1}>
      <CategoryTabs value={tab} onValueChange={setTab}>
        <CategoryTabsList>
          <CategoryTabsTrigger value={CREDENTIAL_TABS[0]}>
            {t("tabs.apiKeys")}
          </CategoryTabsTrigger>
          <CategoryTabsTrigger value={CREDENTIAL_TABS[1]}>
            {t("tabs.oauthApps")}
          </CategoryTabsTrigger>
        </CategoryTabsList>

        <CategoryTabsContent
          value={CREDENTIAL_TABS[0]}
          helpText={t("tabs.apiKeysHelp")}
        >
          <APIKeysTabPanel />
        </CategoryTabsContent>

        <CategoryTabsContent
          value={CREDENTIAL_TABS[1]}
          helpText={t("tabs.oauthAppsHelp")}
        >
          <OAuthAppsTabPanel />
        </CategoryTabsContent>
      </CategoryTabs>
      </div>

      {/* #2129: the OAuth apps the user authorized, with each app's MCP
          default context. Per user, not a credential kind, so it sits
          below the tabs rather than in one. */}
      <div id="connected-apps" tabIndex={-1}><ConnectedAppsSection /></div>
    </PageContainer>
  );
}
