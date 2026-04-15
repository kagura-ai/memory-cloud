"use client";

/**
 * Resource Detail Page
 *
 * Five-tab layout (Overview / Data / Schemas / Tokens / Events) plus a
 * stats strip header. Follows the IA of workspace/contexts/[id]/page.tsx.
 *
 * Issue #47 (initial), Issue #325 (IA reorg — top-level Resources nav
 * with consolidated tabs replacing the orphaned credential routes).
 */

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useTranslations } from "next-intl";
import {
  Activity,
  ArrowLeft,
  BarChart3,
  ChevronRight,
  Clock,
  Database,
  FileJson,
  Key,
  Layers,
} from "lucide-react";
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
import { IndexerStatusPanel } from "@/components/resources/IndexerStatusPanel";
import { ResourceTokensTabPanel } from "@/components/credentials/ResourceTokensTabPanel";
import { CreateSchemaDialog } from "@/components/schemas/CreateSchemaDialog";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import {
  getIndexerStatus,
  type IndexerStatusResponse,
} from "@/lib/api/resources";

const RESOURCE_TABS = [
  "overview",
  "data",
  "schemas",
  "tokens",
  "events",
] as const;

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

  // Indexer status is fetched after the resource loads so a slow/failed
  // indexer endpoint cannot gate the rest of the page. Kept independent of
  // the schema/list fetch deliberately — the panel owns its own loading and
  // error surfaces via ErrorBanner inside the Overview tab.
  const [indexerStatus, setIndexerStatus] = useState<IndexerStatusResponse>();
  const [indexerLoading, setIndexerLoading] = useState(false);
  const [indexerError, setIndexerError] = useState<Error | null>(null);

  // Schema-creation dialog visibility, opened from the Schemas tab EmptyState.
  // State lives on the page (not inside the Schemas tab) so an `onSuccess` can
  // refresh the parent's schema without re-mounting the dialog.
  const [createSchemaOpen, setCreateSchemaOpen] = useState(false);

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
        // Not-found is signalled by `resource === null` (leave `error` unset)
        // so the render path renders `notFoundTitle` + the "not found" copy
        // via the `error || t("detail.notFound")` ErrorBanner fallback, and
        // `isFetchError` stays false for the PageHeader title branch.
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

  const fetchIndexerStatus = useCallback(async () => {
    try {
      setIndexerLoading(true);
      setIndexerError(null);
      const result = await getIndexerStatus(resourceId);
      setIndexerStatus(result);
    } catch (e) {
      setIndexerError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setIndexerLoading(false);
    }
  }, [resourceId]);

  useEffect(() => {
    // Only fetch once the resource is confirmed accessible — avoids spamming
    // 404s from the indexer endpoint when the user hits a resource they
    // cannot see.
    if (!resource) return;
    fetchIndexerStatus();
  }, [resource, fetchIndexerStatus]);

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
    // Separate "fetch failure" (server/network error) from "resource not found"
    // (the workspace simply doesn't have this resource_id) so users don't see
    // a misleading "Resource not found" banner on a 500.
    const isFetchError = !!error;
    return (
      <PageContainer>
        <PageHeader
          title={isFetchError ? t("detail.title") : t("detail.notFoundTitle")}
        />
        <ErrorBanner error={error || t("detail.notFound")} />
        <Button variant="outline" className="mt-4" asChild>
          <Link href="/workspace/resources">
            <ArrowLeft className="h-4 w-4 mr-2" />
            {t("detail.backToList")}
          </Link>
        </Button>
      </PageContainer>
    );
  }

  const displayName =
    resource.context_display_name ||
    resource.context_name ||
    resource.resource_id;

  return (
    <PageContainer>
      {/* Breadcrumb — slightly higher contrast on the parent link to make it
          recognizably tappable, current segment carries weight via mono+bold. */}
      <nav className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 mb-4">
        <Link
          href="/workspace/resources"
          className="hover:text-foreground hover:underline focus:outline-none focus:ring-2 focus:ring-ring rounded"
        >
          {t("list.title")}
        </Link>
        <ChevronRight className="h-4 w-4 text-gray-400" />
        <span className="text-foreground font-mono font-medium">
          {resource.resource_id}
        </span>
      </nav>

      <PageHeader title={displayName} description={resource.resource_id} />

      <div className="mb-8">
        <ResourceStatsStrip resource={resource} />
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        {/* Sticky tab bar — keeps navigation reachable when token tables /
            events get long. The negative margin is paired with horizontal
            padding so the backdrop blur edge meets the page container. */}
        <div className="sticky top-0 z-10 -mx-4 mb-2 bg-background/85 px-4 py-2 backdrop-blur supports-[backdrop-filter]:bg-background/70">
          <TabsList>
            <TabsTrigger value="overview">
              <BarChart3 className="mr-2 h-4 w-4" />
              {t("tabs.overview")}
            </TabsTrigger>
            <TabsTrigger value="data">
              <Database className="mr-2 h-4 w-4" />
              {t("tabs.data")}
            </TabsTrigger>
            <TabsTrigger value="schemas">
              <Layers className="mr-2 h-4 w-4" />
              {t("tabs.schemas")}
            </TabsTrigger>
            <TabsTrigger value="tokens">
              <Key className="mr-2 h-4 w-4" />
              {t("tabs.tokens")}
            </TabsTrigger>
            <TabsTrigger value="events">
              <Activity className="mr-2 h-4 w-4" />
              {t("tabs.events")}
            </TabsTrigger>
          </TabsList>
        </div>

        {/*
          Overview hosts the Indexer Status panel (Issue #326) so ingest
          health is visible to operators without leaving the detail page.
          Schema management lives in the Schemas tab — kept separate because
          schema is a define-time concern and the indexer state is runtime.
        */}
        <TabsContent value="overview" className="mt-6">
          <IndexerStatusPanel
            data={indexerStatus}
            isLoading={indexerLoading}
            error={indexerError}
          />
        </TabsContent>

        <TabsContent value="data" className="mt-6">
          <ResourceDataTabPlaceholder />
        </TabsContent>

        <TabsContent value="schemas" className="mt-6">
          {schema ? (
            // Schema rows are immutable per version (the version + JSONB
            // payload is what indexer_state.active_version + resource_events
            // pin against). "Edit" is therefore "create the next version" —
            // the action wraps the same CreateSchemaDialog used for the
            // first registration, so the operator has one consistent flow.
            <div className="space-y-4">
              <div className="flex items-center justify-between gap-4">
                <div className="text-sm text-muted-foreground">
                  {t("schema.versionLabel", {
                    version: schema.schema_version,
                  })}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCreateSchemaOpen(true)}
                >
                  {t("schema.createNewVersionAction")}
                </Button>
              </div>
              <SchemaFieldTable fields={schema.field_definitions} />
            </div>
          ) : (
            <EmptyState
              icon={FileJson}
              title={t("schema.emptyTitle")}
              description={t("schema.emptyDescription")}
              actionLabel={t("schema.createAction")}
              onAction={() => setCreateSchemaOpen(true)}
              compact
            />
          )}
        </TabsContent>

        <TabsContent value="tokens" className="mt-6">
          <ResourceTokensTabPanel resourceIdFilter={resourceId} />
        </TabsContent>

        <TabsContent value="events" className="mt-6">
          <EmptyState
            icon={Clock}
            title={t("events.comingSoonTitle")}
            description={t("events.comingSoonDescription")}
            compact
          />
        </TabsContent>
      </Tabs>

      <CreateSchemaDialog
        isOpen={createSchemaOpen}
        onClose={() => setCreateSchemaOpen(false)}
        // The dialog is pinned to this page's resource so the picker is
        // hidden and the operator cannot accidentally write the new version
        // against a different slug — the page would not reflect it and the
        // wrong indexer would pick it up.
        lockedResourceId={resourceId}
        onSuccess={(created) => {
          setSchema(created);
          setCreateSchemaOpen(false);
        }}
      />
    </PageContainer>
  );
}
