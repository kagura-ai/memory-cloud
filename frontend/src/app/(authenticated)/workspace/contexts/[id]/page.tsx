/**
 * Consolidated Context Detail Page
 *
 * Issue #232: Consolidate context detail into tabbed layout (Overview / Connections / Settings)
 * Replaces 4 separate routes with one tabbed page using entity Tabs (#225) and useTabParam (#230).
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import Link from "next/link";
import dynamic from "next/dynamic";
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
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole } from "@/lib/auth/rbac";
import {
  Lock,
  Users,
  Globe,
  Check,
  ChevronRight,
  ArrowLeft,
  AlertCircle,
} from "lucide-react";
import { OverviewTabPanel } from "@/components/contexts/OverviewTabPanel";
import { MemoriesTabPanel } from "@/components/contexts/MemoriesTabPanel";
import { ConnectionsTabPanel } from "@/components/contexts/ConnectionsTabPanel";
import { SettingsTabPanel } from "@/components/contexts/SettingsTabPanel";
import { SearchSettingsSection } from "@/components/contexts/SearchSettingsSection";
import { MembersSection } from "@/components/contexts/MembersSection";
import { ProtectionSection } from "@/components/contexts/ProtectionSection";
// Issue #233: graph viz tab — lazy-loaded so d3 modules are code-split.
const GraphTabPanel = dynamic(
  () =>
    import("@/components/contexts/GraphTabPanel").then((m) => ({
      default: m.GraphTabPanel,
    })),
  { ssr: false },
);
// Issue #497: analyses tab — lazy-loaded so the broadlistening bundle
// (recharts placeholder + scatter SVG + modal) stays out of the
// non-owner sessions even though the tab itself is owner-gated upstream.
const AnalysesTabPanel = dynamic(
  () =>
    import("@/components/contexts/analyses/AnalysesTabPanel").then((m) => ({
      default: m.AnalysesTabPanel,
    })),
  { ssr: false },
);

const ADMIN_CONTEXT_TABS = [
  "overview",
  "memories",
  "connections",
  "graph",
  "settings",
] as const;
const OWNER_CONTEXT_TABS = [
  "overview",
  "memories",
  "connections",
  "graph",
  "analyses",
  "settings",
] as const;
const NON_ADMIN_CONTEXT_TABS = ["overview"] as const;

export default function ContextDetailPage() {
  const params = useParams();
  const contextId = params.id as string;
  const t = useTranslations("contextDetail");
  // Issue #497: analyses namespace is top-level, so a second hook
  // call is needed for the tab label. next-intl supports multiple
  // useTranslations() in one component.
  const tAnalyses = useTranslations("analyses");
  const { user } = useAuth();
  const { currentContext } = useMemoryContext();
  const { currentWorkspace } = useWorkspace();

  // Issue #398: member/viewer only see Overview. Admin+ see all four tabs.
  // Issue #497: owner additionally sees the Analyses tab.
  // hasWorkspaceRole returns false while currentWorkspace hydrates — the
  // admin tabs stay hidden until role is known, preventing a flash where
  // a member briefly sees admin-only triggers.
  const canSeeAdminTabs = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    "admin",
  );
  // Analyses tab gating: requires owner role AND workspace allowlist
  // membership (#497). Hiding the tab entirely for non-allowlisted owners
  // keeps the UX honest — there is no path forward inside the tab until
  // the workspace is on the allowlist.
  const canSeeAnalysesTab =
    hasWorkspaceRole(currentWorkspace?.current_user_role, "owner") &&
    currentWorkspace?.analyses_enabled === true;
  const visibleTabs = canSeeAnalysesTab
    ? OWNER_CONTEXT_TABS
    : canSeeAdminTabs
      ? ADMIN_CONTEXT_TABS
      : NON_ADMIN_CONTEXT_TABS;

  // Passing visibleTabs as allowedValues makes useTabParam clamp unknown
  // URL values (e.g. member lands on ?tab=settings via deep-link) back to
  // "overview" — no separate redirect effect needed for the display. We
  // still snap the URL below so the address bar matches what's rendered.
  const [tab, setTab] = useTabParam("overview", "tab", visibleTabs);
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

  // Snap the URL to ?tab=overview when a member/viewer arrives on an
  // admin-only tab via deep-link. useTabParam's allowedValues already clamps
  // the *rendered* value to overview, but only auto-promotes the URL when the
  // param is absent — not when it's present-but-invalid. Compare the raw URL
  // value here; reading `tab` would always see "overview" for non-admins so
  // the snap would never fire.
  //
  // The `!currentWorkspace` guard is load-bearing: during WorkspaceContext
  // hydration `currentWorkspace` is null, which collapses canSeeAdminTabs to
  // false. Without the guard, an admin hard-reloading ?tab=settings would
  // have their URL snapped back to overview before the role resolved.
  const searchParams = useSearchParams();
  const rawTab = searchParams.get("tab");
  useEffect(() => {
    if (!currentWorkspace) return;
    if (!canSeeAdminTabs && rawTab && rawTab !== "overview") {
      setTab("overview");
    }
  }, [currentWorkspace, canSeeAdminTabs, rawTab, setTab]);

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

  // Mirror the icon + color choice on the contexts list page so the title
  // glyph is the same one the user just clicked through.
  // - Public  → Globe (purple)
  // - Private → Lock  (blue)
  // - Shared  → Users (green)
  const privacyIcon = context.is_public ? (
    <Globe
      className="h-5 w-5 text-purple-600 dark:text-purple-400 inline-block"
      aria-label={t("publicContext")}
    />
  ) : context.is_private ? (
    <Lock
      className="h-5 w-5 text-blue-600 dark:text-blue-400 inline-block"
      aria-label={t("privateContext")}
    />
  ) : (
    <Users
      className="h-5 w-5 text-green-600 dark:text-green-400 inline-block"
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
            {context.sleep_mode !== "full" && (
              <Badge variant="outline">
                {
                  {
                    edges_only: t("sleepModeBadgeEdgesOnly"),
                    skip: t("sleepModeBadgeSkip"),
                  }[context.sleep_mode]
                }
              </Badge>
            )}
          </div>
        }
      />

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">{t("tabs.overview")}</TabsTrigger>
          {/* Issue #398: memories/connections/graph tabs are admin-only.
              Settings is rendered separately below so the analyses tab
              (owner-only, #497) can land between graph and settings —
              matching the ordering in OWNER_CONTEXT_TABS. */}
          {canSeeAdminTabs && (
            <>
              <TabsTrigger value="memories">{t("tabs.memories")}</TabsTrigger>
              <TabsTrigger value="connections">
                {t("tabs.connections")}
              </TabsTrigger>
              <TabsTrigger value="graph">{t("tabs.graph")}</TabsTrigger>
            </>
          )}
          {/* Analyses tab is owner-only AND requires allowlist membership.
              Owners whose workspace is not on the allowlist do not see
              the tab at all (#497 — operator preference). */}
          {canSeeAnalysesTab && (
            <TabsTrigger value="analyses">{tAnalyses("tabLabel")}</TabsTrigger>
          )}
          {/* Settings stays last — admin-only. */}
          {canSeeAdminTabs && (
            <TabsTrigger value="settings">{t("tabs.settings")}</TabsTrigger>
          )}
        </TabsList>

        <TabsContent value="overview">
          <OverviewTabPanel contextId={contextId} context={context} />
        </TabsContent>

        {/* TabsContent for admin-only tabs is also gated so the panels never
            render for member/viewer (eliminates one-frame flash on direct
            ?tab=settings deep-link before the URL snap effect fires, and
            keeps GraphTabPanel's d3 bundle out of non-admin sessions). */}
        {canSeeAdminTabs && (
          <>
            <TabsContent value="memories">
              <MemoriesTabPanel contextId={contextId} />
            </TabsContent>

            <TabsContent value="connections">
              <ConnectionsTabPanel contextId={contextId} />
            </TabsContent>

            <TabsContent value="graph">
              <GraphTabPanel contextId={contextId} />
            </TabsContent>

            <TabsContent value="settings">
              <SettingsTabPanel
                contextId={contextId}
                context={context}
                onContextUpdated={handleContextUpdated}
              />
              <Separator className="my-8" />
              <SearchSettingsSection contextId={contextId} />
              {!context.is_private && (
                <>
                  <Separator className="my-8" />
                  <MembersSection contextId={contextId} context={context} />
                </>
              )}
              <Separator className="my-8" />
              <ProtectionSection context={context} />
            </TabsContent>
          </>
        )}
        {/* Analyses TabsContent gated on tab visibility so the scatter
            bundle never enters non-owner / non-allowlisted sessions. */}
        {canSeeAnalysesTab && (
          <TabsContent value="analyses">
            <AnalysesTabPanel
              contextId={contextId}
              contextName={context.display_name || context.name}
            />
          </TabsContent>
        )}
      </Tabs>
    </PageContainer>
  );
}
