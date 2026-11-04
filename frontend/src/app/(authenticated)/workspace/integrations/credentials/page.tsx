"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { ContextualHelp } from "@/components/common/ContextualHelp";
import { WorkspacePageHeader as PageHeader } from "@/components/dashboard/WorkspacePageHeader";
import { FeatureGuide } from "@/components/common/FeatureGuide";
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
  const navigation = useTranslations("sectionNavigation");
  const [tab] = useTabParam("api-keys", "tab", CREDENTIAL_TABS);

  return (
    <PageContainer>
      <PageHeader title={navigation(tab === "oauth-apps" ? "oauthApps" : "apiKeys")} description={t("description")} />

      <Link href="/workspace/integrations" className="inline-block py-2 text-sm text-primary underline">{entry("back")}</Link>
      <ContextualHelp topic="credentials" />
      <FeatureGuide storageKey="credentials" title={t("featureGuide.title")}>
        <p>{t("featureGuide.overview")}</p>
        <p>{t("featureGuide.choosingMethod")}</p>
      </FeatureGuide>

      <div id="mcp-connection" tabIndex={-1}><McpConnectionCard /></div>

      <div id="credential-tools" tabIndex={-1}>
        <p className="mb-3 text-sm text-muted-foreground">{t(tab === "oauth-apps" ? "tabs.oauthAppsHelp" : "tabs.apiKeysHelp")}</p>
        {tab === "oauth-apps" ? <OAuthAppsTabPanel /> : <APIKeysTabPanel />}
      </div>

      {/* #2129: the OAuth apps the user authorized, with each app's MCP
          default context. Per user, not a credential kind, so it sits
          below the tabs rather than in one. */}
      <div id="connected-apps" tabIndex={-1}><ConnectedAppsSection /></div>
    </PageContainer>
  );
}
