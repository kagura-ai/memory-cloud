/**
 * Consolidated Context Detail Page
 *
 * Issue #232: Consolidate context detail into tabbed layout (Overview / Connections / Settings)
 * Replaces 4 separate routes with one tabbed page using entity Tabs (#225) and useTabParam (#230).
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Separator } from "@/components/ui/separator";
import { useTabParam } from "@/hooks/useTabParam";
import { getContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { useMemoryContext } from "@/contexts/MemoryContextContext";
import { useAuth } from "@/contexts/AuthContext";
import {
  Lock,
  Users,
  Check,
  ChevronRight,
  ArrowLeft,
  AlertCircle,
} from "lucide-react";
import { OverviewTabPanel } from "@/components/contexts/OverviewTabPanel";
import { ConnectionsTabPanel } from "@/components/contexts/ConnectionsTabPanel";
import { SettingsTabPanel } from "@/components/contexts/SettingsTabPanel";
import { SearchSettingsSection } from "@/components/contexts/SearchSettingsSection";
import { ProtectionSection } from "@/components/contexts/ProtectionSection";

export default function ContextDetailPage() {
  const params = useParams();
  const contextId = params.id as string;
  const t = useTranslations("contextDetail");
  const { user } = useAuth();
  const { currentContext } = useMemoryContext();

  const [tab, setTab] = useTabParam("overview");
  const [context, setContext] = useState<Context | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchContext = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const ctx = await getContext(contextId);
      setContext(ctx);
    } catch {
      setError(t("failedToLoad"));
    } finally {
      setLoading(false);
    }
  }, [contextId, t]);

  useEffect(() => {
    fetchContext();
  }, [fetchContext]);

  useEffect(() => {
    const title = context?.display_name || context?.name || t("title");
    document.title = `${title} - Kagura Memory Cloud`;
  }, [context, t]);

  const handleContextUpdated = useCallback((updated: Context) => {
    setContext(updated);
  }, []);

  if (loading) {
    return (
      <PageContainer>
        <SpinnerLoading size="lg" message={t("loading")} />
      </PageContainer>
    );
  }

  if (error || !context) {
    return (
      <PageContainer>
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>{t("notFoundTitle")}</AlertTitle>
          <AlertDescription>
            {error || t("notFoundDescription")}
          </AlertDescription>
        </Alert>
        <Link href="/workspace/contexts">
          <Button variant="outline" className="mt-4">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("backToContexts")}
          </Button>
        </Link>
      </PageContainer>
    );
  }

  const isCurrent = currentContext?.id === context.id;
  const displayName = context.display_name || context.name;

  const privacyIcon = context.is_private ? (
    <Lock
      className="h-5 w-5 text-gray-400 inline-block"
      aria-label={t("privateContext")}
    />
  ) : (
    <Users
      className="h-5 w-5 text-blue-500 inline-block"
      aria-label={t("sharedContext")}
    />
  );

  const pageTitle = (
    <div className="flex items-center gap-2">
      {privacyIcon}
      <span>{displayName}</span>
    </div>
  );

  return (
    <PageContainer>
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-4">
        <Link
          href="/workspace/contexts"
          className="hover:text-gray-900 dark:hover:text-gray-200 hover:underline"
        >
          {t("breadcrumbContexts")}
        </Link>
        <ChevronRight className="h-4 w-4" />
        <div className="flex items-center gap-2">
          <span className="text-gray-900 dark:text-gray-100">
            {displayName}
          </span>
          {isCurrent && (
            <span className="px-2 py-0.5 text-xs bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full font-medium flex items-center gap-1">
              <Check className="h-3 w-3" />
              {t("current")}
            </span>
          )}
        </div>
      </nav>

      <PageHeader
        title={pageTitle}
        actions={
          <div className="flex items-center gap-2">
            {contextId === user?.current_context_id && (
              <Badge variant="default" className="bg-brand-green-600">
                {t("current")}
              </Badge>
            )}
            {context.is_default && (
              <Badge variant="secondary">{t("default")}</Badge>
            )}
          </div>
        }
      />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">{t("tabs.overview")}</TabsTrigger>
          <TabsTrigger value="connections">{t("tabs.connections")}</TabsTrigger>
          <TabsTrigger value="settings">{t("tabs.settings")}</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <OverviewTabPanel contextId={contextId} context={context} />
        </TabsContent>

        <TabsContent value="connections">
          <ConnectionsTabPanel contextId={contextId} />
        </TabsContent>

        <TabsContent value="settings">
          <SettingsTabPanel
            contextId={contextId}
            context={context}
            onContextUpdated={handleContextUpdated}
          />
          <Separator className="my-8" />
          <SearchSettingsSection contextId={contextId} />
          <Separator className="my-8" />
          <ProtectionSection context={context} />
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}
