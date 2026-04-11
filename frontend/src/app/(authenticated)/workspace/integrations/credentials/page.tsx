"use client";

import { Suspense } from "react";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { FeatureGuide } from "@/components/common/FeatureGuide";
import { LoadingState } from "@/components/common/LoadingState";
import {
  CategoryTabs,
  CategoryTabsList,
  CategoryTabsTrigger,
  CategoryTabsContent,
} from "@/components/ui/tabs";
import { useTabParam } from "@/hooks/useTabParam";
import { APIKeysTabPanel } from "@/components/credentials/APIKeysTabPanel";
import { OAuthAppsTabPanel } from "@/components/credentials/OAuthAppsTabPanel";
import { ResourceTokensTabPanel } from "@/components/credentials/ResourceTokensTabPanel";

const CREDENTIAL_TABS = ["api-keys", "oauth-apps", "resource-tokens"] as const;

export default function CredentialsPage() {
  const t = useTranslations("credentials");
  const [tab, setTab] = useTabParam("api-keys", "tab", CREDENTIAL_TABS);

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      <FeatureGuide storageKey="credentials" title={t("featureGuide.title")}>
        <p>{t("featureGuide.overview")}</p>
        <p>{t("featureGuide.choosingMethod")}</p>
      </FeatureGuide>

      <CategoryTabs value={tab} onValueChange={setTab}>
        <CategoryTabsList>
          <CategoryTabsTrigger value={CREDENTIAL_TABS[0]}>
            {t("tabs.apiKeys")}
          </CategoryTabsTrigger>
          <CategoryTabsTrigger value={CREDENTIAL_TABS[1]}>
            {t("tabs.oauthApps")}
          </CategoryTabsTrigger>
          <CategoryTabsTrigger value={CREDENTIAL_TABS[2]}>
            {t("tabs.resourceTokens")}
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

        <CategoryTabsContent
          value={CREDENTIAL_TABS[2]}
          helpText={t("tabs.resourceTokensHelp")}
        >
          <ResourceTokensTabPanel />
        </CategoryTabsContent>
      </CategoryTabs>
    </PageContainer>
  );
}
