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

export default function CredentialsPage() {
  const t = useTranslations("credentials");
  const [tab, setTab] = useTabParam("api-keys");

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      <FeatureGuide storageKey="credentials" title={t("featureGuide.title")}>
        <p>{t("featureGuide.overview")}</p>
        <p>{t("featureGuide.choosingMethod")}</p>
      </FeatureGuide>

      <CategoryTabs value={tab} onValueChange={setTab}>
        <CategoryTabsList>
          <CategoryTabsTrigger value="api-keys">
            {t("tabs.apiKeys")}
          </CategoryTabsTrigger>
          <CategoryTabsTrigger value="oauth-apps">
            {t("tabs.oauthApps")}
          </CategoryTabsTrigger>
          <CategoryTabsTrigger value="resource-tokens">
            {t("tabs.resourceTokens")}
          </CategoryTabsTrigger>
        </CategoryTabsList>

        <CategoryTabsContent value="api-keys" helpText={t("tabs.apiKeysHelp")}>
          <APIKeysTabPanel />
        </CategoryTabsContent>

        <CategoryTabsContent
          value="oauth-apps"
          helpText={t("tabs.oauthAppsHelp")}
        >
          <OAuthAppsTabPanel />
        </CategoryTabsContent>

        <CategoryTabsContent
          value="resource-tokens"
          helpText={t("tabs.resourceTokensHelp")}
        >
          <ResourceTokensTabPanel />
        </CategoryTabsContent>
      </CategoryTabs>
    </PageContainer>
  );
}
