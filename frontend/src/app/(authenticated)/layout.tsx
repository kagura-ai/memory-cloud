"use client";

/**
 * Dashboard Layout
 *
 * Provides the main layout for authenticated dashboard pages.
 * Includes sidebar navigation, header, and authentication guard.
 * Issue #651, #655 - Unified landing and dashboard handling
 * Issue #115 Phase B-5: Workspace requirement check
 * Issue #1665: blocking terms re-acceptance step
 */

import { Fragment, Suspense, useEffect, useState } from "react";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { useLocale } from "@/i18n";
import { WorkspaceProvider, useWorkspace } from "@/contexts/WorkspaceContext";
import { MemoryContextProvider } from "@/contexts/MemoryContextContext";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useTranslations } from "next-intl";
import { WorkspacePageNavigationProvider } from "@/components/dashboard/WorkspacePageHeader";
import { FeatureRouteBoundary } from "@/components/dashboard/FeatureRouteBoundary";
import { Sidebar } from "@/components/dashboard/Sidebar";
import { WorkspaceSelectionScreen } from "@/components/workspaces/WorkspaceSelectionScreen";
import { TermsReacceptanceDialog } from "@/components/auth/TermsReacceptanceDialog";
import { CapacityLockBanner } from "@/components/common/CapacityLockBanner";
import { WorkspaceSuspensionNotice } from "@/components/plan/WorkspaceSuspensionNotice";

/**
 * Issue #1665: shown INSTEAD of the app while `/auth/me` reports
 * `terms_acceptance_required` — the deployment's terms version changed since
 * this user last accepted. Nothing behind it mounts (no page fetches, no
 * workspace guard) until they accept or sign out. The version comes from
 * `/auth/me` itself, so a failed `/system/info` cannot strand the user here.
 */
function TermsReacceptanceGate() {
  const { user, refetchUser, logout } = useAuth();
  const termsVersion = user?.terms_version ?? undefined;
  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900">
      <TermsReacceptanceDialog
        termsVersion={termsVersion}
        onAccepted={async () => { await refetchUser(); }}
        onSignOut={() => void logout()}
      />
    </div>
  );
}

/**
 * Component to check workspace requirement and redirect if needed
 * Issue #276: Show workspace selection screen for users with multiple workspaces
 */
function WorkspaceGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const t = useTranslations("featureAccess");
  const { workspaces, currentWorkspace, loading, loadFailed, switchWorkspace } =
    useWorkspace();
  // A stale or removed workspace must not silently send a billing action to another.
  const needsSelection = workspaces.length > 0 && !workspaces.some(w => w.id === currentWorkspace?.id);

  // Pages that don't require an workspace. /workspace/settings is a
  // compatibility-only route that immediately redirects to
  // /workspace/settings/general (see workspace/settings/page.tsx), so both
  // paths must be exempted — otherwise zero-workspace users land on
  // /workspace/settings/general?create=true and the guard pushes them back,
  // producing a redirect loop (Copilot review on PR #662).
  const isWorkspaceCreationPage =
    (pathname === "/workspace/settings" ||
      pathname === "/workspace/settings/general") &&
    searchParams.get("create") === "true";
  const isSystemRoute = pathname === "/admin" || pathname.startsWith("/admin/");
  const isPublicPage =
    pathname === "/pricing" || pathname === "/login" || pathname === "/";

  useEffect(() => {
    // Wait for loading to complete
    if (loading || loadFailed) return;

    // Skip check for workspace creation page and public pages
    if (isWorkspaceCreationPage || isPublicPage || isSystemRoute) return;

    // Issue #246 / #660: When the user has zero workspaces, send them
    // directly to the canonical create-form route. /workspace/settings
    // immediately redirects to /workspace/settings/general for backwards
    // compatibility — pushing here saves one redirect hop and avoids
    // depending on the redirect intermediate (Copilot review on PR #662).
    if (workspaces.length === 0) {
      router.push("/workspace/settings/general?create=true");
      return;
    }

  }, [
    loading,
    loadFailed,
    workspaces,
    isWorkspaceCreationPage,
    isPublicPage,
    isSystemRoute,
    router,
  ]);

  // Platform administration depends on system role, never workspace membership.
  if (isSystemRoute) return <>{children}</>;

  // Always show loading spinner while checking workspaces (prevents flash of main page)
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 dark:bg-slate-900">
        <div className="h-10 w-10 rounded-full border-4 border-slate-200 border-t-slate-600 animate-spin" />
      </div>
    );
  }

  if (loadFailed && !isWorkspaceCreationPage) {
    return <Alert><AlertDescription>{t("workspaceLoadFailed")}{" "}
      <Button variant="outline" onClick={() => window.location.reload()}>{t("retry")}</Button>
    </AlertDescription></Alert>;
  }

  // If no workspaces and not on create page, show loading (redirect will happen)
  if (workspaces.length === 0 && !isWorkspaceCreationPage && !isPublicPage) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 dark:bg-slate-900">
        <div className="h-10 w-10 rounded-full border-4 border-slate-200 border-t-slate-600 animate-spin" />
      </div>
    );
  }

  // Issue #276: Show workspace selection screen
  if (needsSelection && !isWorkspaceCreationPage && !isPublicPage) {
    return (
      <WorkspaceSelectionScreen
        workspaces={workspaces}
        onSelect={switchWorkspace}
        onCreateNew={() => router.push("/workspace/settings?create=true")}
      />
    );
  }

  return <>{children}</>;
}

// System routes do not need a workspace, but their surrounding chrome does.
function ScopedSidebar() {
  const { currentWorkspaceId, loading, loadFailed } = useWorkspace();
  return loading || loadFailed ? null : <Sidebar key={currentWorkspaceId ?? "unselected"} />;
}

function WorkspaceBanners() {
  const { currentWorkspaceId, loading, loadFailed } = useWorkspace();
  if (!currentWorkspaceId || loading || loadFailed) return null;
  return <Fragment key={currentWorkspaceId}><CapacityLockBanner /><WorkspaceSuspensionNotice /></Fragment>;
}

function DashboardContent({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth();
  const { locale: uiLocale, setLocale: setUiLocale } = useLocale();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  // The first route this layout shows is where the user landed (sign-in, a
  // link, a reload); any later route was navigated to inside the app.
  const [landingPath, setLandingPath] = useState<string | null>(pathname);
  if (landingPath !== null && landingPath !== pathname) setLandingPath(null);

  // The signed-in user's saved `locale` is the source of truth for the UI
  // language. The i18n provider only hydrates from localStorage, so without
  // this sync the Profile > Language preference never reaches the interface
  // (it would just sit in the backend record). Apply it whenever it differs.
  useEffect(() => {
    const pref = user?.locale;
    if ((pref === "en" || pref === "ja") && pref !== uiLocale) {
      setUiLocale(pref);
    }
  }, [user?.locale, uiLocale, setUiLocale]);

  // Check if on workspace creation page. Mirrors WorkspaceGuard's logic so the
  // create form renders in the minimal layout even after the compatibility
  // redirect from /workspace/settings → /workspace/settings/general (Copilot
  // review on PR #662 loop 3).
  const isWorkspaceCreationPage =
    (pathname === "/workspace/settings" ||
      pathname === "/workspace/settings/general") &&
    searchParams.get("create") === "true";

  // Redirect to login for protected routes (not homepage)
  useEffect(() => {
    if (!isLoading && !user && pathname !== "/") {
      router.push("/login");
    }
  }, [user, isLoading, pathname, router]);

  // Show loading spinner while checking authentication
  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-10 w-10 rounded-full border-4 border-slate-200 border-t-slate-600 animate-spin" />
      </div>
    );
  }

  // For unauthenticated users on homepage (/), show landing page without dashboard chrome
  if (!user && pathname === "/") {
    return <>{children}</>;
  }

  // For protected routes, don't render if not authenticated (redirecting to login)
  if (!user) {
    return null;
  }

  // Issue #1665: the updated terms must be accepted before anything else.
  if (user.terms_acceptance_required) {
    return <TermsReacceptanceGate />;
  }

  const isMockAuth =
    process.env.NODE_ENV === "development" &&
    process.env.NEXT_PUBLIC_ENABLE_MOCK_AUTH === "true";

  // Minimal layout for workspace creation page
  if (isWorkspaceCreationPage) {
    return (
      <WorkspaceProvider>
        <MemoryContextProvider>
          <div className="min-h-screen bg-slate-50 dark:bg-slate-900">
            {/* Development Warning Banner */}
            {isMockAuth && (
              <div className="border-b border-orange-200 bg-gradient-to-r from-orange-100 to-yellow-100 px-4 py-2">
                <p className="text-center text-sm font-medium text-orange-900">
                  ⚠️ Development Mode: Mock Authentication Enabled (User:{" "}
                  {user.name})
                </p>
              </div>
            )}

            <main className="container mx-auto p-6 max-w-4xl">{children}</main>
          </div>
        </MemoryContextProvider>
      </WorkspaceProvider>
    );
  }

  // Full dashboard layout with workspace guard
  return (
    <WorkspaceProvider>
      <WorkspaceGuard>
        <MemoryContextProvider>
          <div className="flex h-screen overflow-hidden">
            {/* Sidebar */}
            <ScopedSidebar />

            {/* Main Content Area */}
            <div className="flex flex-col flex-1 overflow-hidden">
              {/* Development Warning Banner */}
              {isMockAuth && (
                <div className="border-b border-orange-200 bg-gradient-to-r from-orange-100 to-yellow-100 px-4 py-2">
                  <p className="text-center text-sm font-medium text-orange-900">
                    ⚠️ Development Mode: Mock Authentication Enabled (User:{" "}
                    {user.name})
                  </p>
                </div>
              )}

              <WorkspaceBanners />

              {/* Page Content */}
              <main className="flex-1 overflow-y-auto bg-slate-50 dark:bg-slate-900">
                <div className="container mx-auto p-6"><WorkspacePageNavigationProvider><FeatureRouteBoundary landing={landingPath === pathname}>{children}</FeatureRouteBoundary></WorkspacePageNavigationProvider></div>
              </main>
            </div>
          </div>
        </MemoryContextProvider>
      </WorkspaceGuard>
    </WorkspaceProvider>
  );
}

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center min-h-screen">
          Loading...
        </div>
      }
    >
      <DashboardContent>{children}</DashboardContent>
    </Suspense>
  );
}
