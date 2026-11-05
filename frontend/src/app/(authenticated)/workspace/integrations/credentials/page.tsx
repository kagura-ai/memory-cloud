"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { ContextualHelp } from "@/components/common/ContextualHelp";
import { WorkspacePageHeader as PageHeader } from "@/components/dashboard/WorkspacePageHeader";
import { useTabParam } from "@/hooks/useTabParam";
import { McpConnectionHelp } from "@/components/credentials/McpConnectionHelp";
import { APIKeysTabPanel } from "@/components/credentials/APIKeysTabPanel";
import { ConnectedAppsSection } from "@/components/credentials/ConnectedAppsSection";
import { OAuthAppsTabPanel } from "@/components/credentials/OAuthAppsTabPanel";

const CREDENTIAL_TABS = ["api-keys", "oauth-apps", "connected-apps"] as const;

export default function CredentialsPage() {
  const router = useRouter();
  const navigation = useTranslations("sectionNavigation");
  const [tab, setTab] = useTabParam("api-keys", "tab", CREDENTIAL_TABS);

  useEffect(() => {
    const syncHash = () => {
      if (window.location.hash === "#mcp-connection") router.replace("/workspace/integrations#mcp-connection");
      // Preserve bookmarks to the former shared section.
      if (window.location.hash === "#connected-apps" && tab !== "connected-apps") {
        setTab("connected-apps");
      }
    };
    syncHash();
    window.addEventListener("hashchange", syncHash);
    return () => window.removeEventListener("hashchange", syncHash);
  }, [tab, setTab, router]);

  return (
    <PageContainer>
      <PageHeader title={navigation("integrations")} />
      <ContextualHelp topic="credentials" />
      {tab === "oauth-apps" && (
        <div className="mb-4"><McpConnectionHelp /></div>
      )}

      <div id="credential-tools" tabIndex={-1}>
        {tab === "connected-apps" ? (
          <div id="connected-apps" tabIndex={-1}><ConnectedAppsSection /></div>
        ) : tab === "oauth-apps" ? (
          <OAuthAppsTabPanel />
        ) : (
          <APIKeysTabPanel />
        )}
      </div>
    </PageContainer>
  );
}
