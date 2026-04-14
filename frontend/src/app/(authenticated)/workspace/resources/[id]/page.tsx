"use client";

/**
 * Resource Detail Page
 *
 * Two-tab layout (Overview = schema viewer, Data = #316 placeholder) plus a
 * stats strip header. Follows the IA of workspace/contexts/[id]/page.tsx.
 *
 * Issue #47
 */

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { ArrowLeft, ChevronRight, Database, FileJson } from "lucide-react";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useTabParam } from "@/hooks/useTabParam";
import { listResources, type ResourceListItem } from "@/lib/api/resources";
import { getSchema, type ResourceSchema } from "@/lib/api/schemas";
import { ApiError } from "@/lib/api/base";
import { ResourceStatsStrip } from "@/components/resources/ResourceStatsStrip";
import { SchemaFieldTable } from "@/components/resources/SchemaFieldTable";
import { ResourceDataTabPlaceholder } from "@/components/resources/ResourceDataTabPlaceholder";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { useWorkspace } from "@/contexts/WorkspaceContext";

const RESOURCE_TABS = ["overview", "data"] as const;

export default function ResourceDetailPage() {
  const params = useParams();
  const router = useRouter();
  // Next.js App Router's useParams() returns route segments already decoded —
  // calling decodeURIComponent here is redundant and throws URIError on any
  // legitimate literal `%` (e.g., an encoded `%25` becomes `%` after useParams).
  const resourceId = params.id as string;
  const t = useTranslations("resources");
  const { currentWorkspace } = useWorkspace();

  // Match the list page's plan-gate posture — a deep-link direct to this URL
  // on a Free/Basic workspace must see the upgrade CTA, not a generic
  // not-found/error after a wasted API round-trip.
  const planName = currentWorkspace?.plan_name;
  const workspaceReady = currentWorkspace !== null && planName !== undefined;
  const isPlanGated =
    workspaceReady && (planName === "free" || planName === "basic");

  const [tab, setTab] = useTabParam("overview", "tab", RESOURCE_TABS);

  const [resource, setResource] = useState<ResourceListItem | null>(null);
  const [schema, setSchema] = useState<ResourceSchema | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchResource = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      // Parallel fetch: list (for the stats row) and schema (for the Overview tab)
      // are independent. List-then-find is acceptable at < 50 rows/workspace; if
      // that invariant breaks, replace with a dedicated /resources/{id} endpoint.
      const [listResult, schemaResult] = await Promise.allSettled([
        listResources(),
        getSchema(resourceId),
      ]);

      if (listResult.status !== "fulfilled") {
        throw listResult.reason;
      }
      const found = listResult.value.resources.find(
        (r) => r.resource_id === resourceId,
      );
      if (!found) {
        setError(t("detail.notFound"));
        return;
      }
      setResource(found);

      // Schema is optional but only a 404 counts as "no schema yet"; other
      // errors (auth, network, 5xx) should surface so we don't misleadingly
      // show the "No schema registered" EmptyState while the server is broken.
      if (schemaResult.status === "fulfilled") {
        setSchema(schemaResult.value);
      } else {
        const err = schemaResult.reason;
        if (err instanceof ApiError && err.status === 404) {
          setSchema(null);
        } else {
          throw err;
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : t("detail.fetchError"));
    } finally {
      setLoading(false);
    }
  }, [resourceId, t]);

  useEffect(() => {
    // Hold the fetch until WorkspaceContext hydrates, then skip entirely for
    // plan-gated workspaces — same rule as the list page so a direct deep-link
    // on Free/Basic never fires an API call.
    if (!workspaceReady) return;
    if (isPlanGated) {
      setLoading(false);
      return;
    }
    fetchResource();
  }, [fetchResource, isPlanGated, workspaceReady]);

  useEffect(() => {
    const title = resource
      ? resource.context_display_name ||
        resource.context_name ||
        resource.resource_id
      : t("detail.title");
    document.title = `${title} - Kagura Memory Cloud`;
  }, [resource, t]);

  if (isPlanGated) {
    return (
      <PageContainer>
        <PageHeader title={t("list.title")} />
        <EmptyState
          icon={Database}
          title={t("planGate.title")}
          description={t("planGate.description")}
          actionLabel={t("planGate.action")}
          onAction={() => router.push("/workspace/settings/billing")}
        />
      </PageContainer>
    );
  }

  if (loading) {
    return (
      <PageContainer>
        <SpinnerLoading size="lg" message={t("detail.loading")} />
      </PageContainer>
    );
  }

  if (error || !resource) {
    return (
      <PageContainer>
        <PageHeader title={t("detail.notFoundTitle")} />
        <ErrorBanner error={error || t("detail.notFound")} />
        <Link href="/workspace/resources">
          <Button variant="outline" className="mt-4">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("detail.backToList")}
          </Button>
        </Link>
      </PageContainer>
    );
  }

  const displayName =
    resource.context_display_name ||
    resource.context_name ||
    resource.resource_id;

  return (
    <PageContainer>
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 mb-4">
        <Link
          href="/workspace/resources"
          className="hover:text-gray-900 dark:hover:text-gray-200 hover:underline"
        >
          {t("list.title")}
        </Link>
        <ChevronRight className="h-4 w-4" />
        <span className="text-gray-900 dark:text-gray-100 font-mono">
          {resource.resource_id}
        </span>
      </nav>

      <PageHeader title={displayName} description={resource.resource_id} />

      <div className="mb-6">
        <ResourceStatsStrip resource={resource} />
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">{t("tabs.overview")}</TabsTrigger>
          <TabsTrigger value="data">{t("tabs.data")}</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="mt-6">
          {schema ? (
            <div className="space-y-4">
              <div className="text-sm text-muted-foreground">
                {t("schema.versionLabel", { version: schema.schema_version })}
              </div>
              <SchemaFieldTable fields={schema.field_definitions} />
            </div>
          ) : (
            <EmptyState
              icon={FileJson}
              title={t("schema.emptyTitle")}
              description={t("schema.emptyDescription")}
            />
          )}
        </TabsContent>

        <TabsContent value="data" className="mt-6">
          <ResourceDataTabPlaceholder />
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}
