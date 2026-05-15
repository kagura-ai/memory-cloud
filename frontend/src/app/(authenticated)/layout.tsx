"use client";

/**
 * Dashboard Layout
 *
 * Provides the main layout for authenticated dashboard pages.
 * Includes sidebar navigation, header, and authentication guard.
 * Issue #651, #655 - Unified landing and dashboard handling
 * Issue #115 Phase B-5: Workspace requirement check
 */

import { Suspense, useEffect, useState } from "react";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { WorkspaceProvider, useWorkspace } from "@/contexts/WorkspaceContext";
import { MemoryContextProvider } from "@/contexts/MemoryContextContext";
import { Sidebar } from "@/components/dashboard/Sidebar";
import { Header } from "@/components/dashboard/Header";
import { WorkspaceSelectionScreen } from "@/components/workspaces/WorkspaceSelectionScreen";

/**
 * Component to check workspace requirement and redirect if needed
 * Issue #276: Show workspace selection screen for users with multiple workspaces
 */
function WorkspaceGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { workspaces, currentWorkspace, loading, switchWorkspace } =
    useWorkspace();
  const [showSelection, setShowSelection] = useState(false);

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
  const isPublicPage =
    pathname === "/pricing" || pathname === "/login" || pathname === "/";

  useEffect(() => {
    // Wait for loading to complete
    if (loading) return;

    // Skip check for workspace creation page and public pages
    if (isWorkspaceCreationPage || isPublicPage) return;

    // Issue #246 / #660: When the user has zero workspaces, send them
    // directly to the canonical create-form route. /workspace/settings
    // immediately redirects to /workspace/settings/general for backwards
    // compatibility — pushing here saves one redirect hop and avoids
    // depending on the redirect intermediate (Copilot review on PR #662).
    if (workspaces.length === 0) {
      router.push("/workspace/settings/general?create=true");
      return;
    }

    // Issue #276: Handle workspace selection logic
    // Edge case: currentWorkspace is set but doesn't exist in workspaces list (deleted)
    const currentWorkspaceExists =
      currentWorkspace && workspaces.some((w) => w.id === currentWorkspace.id);

    if (!currentWorkspaceExists && workspaces.length > 0) {
      // Current workspace was deleted or invalid - reset selection
      if (workspaces.length > 1) {
        setShowSelection(true);
      } else if (workspaces.length === 1) {
        // Auto-switch to the only available workspace
        switchWorkspace(workspaces[0].id).catch((error) => {
          console.error(
            "Failed to auto-switch after current workspace invalidated:",
            error,
          );
          router.push("/workspace/dashboard");
        });
      }
    } else if (workspaces.length > 1 && !currentWorkspace) {
      // Multiple workspaces but no current selection
      setShowSelection(true);
    } else if (workspaces.length === 1 && !currentWorkspace) {
      // Single workspace but not selected - auto-switch
      switchWorkspace(workspaces[0].id).catch((error) => {
        console.error("Failed to auto-switch to only workspace:", error);
        router.push("/workspace/dashboard");
      });
    } else {
      setShowSelection(false);
    }
  }, [
    loading,
    workspaces,
    currentWorkspace,
    isWorkspaceCreationPage,
    isPublicPage,
    router,
  ]);

  // Always show loading spinner while checking workspaces (prevents flash of main page)
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 dark:bg-slate-900">
        <div className="h-10 w-10 rounded-full border-4 border-slate-200 border-t-slate-600 animate-spin" />
      </div>
    );
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
  if (showSelection && workspaces.length > 1) {
    return (
      <WorkspaceSelectionScreen
        workspaces={workspaces}
        onSelect={async (workspaceId) => {
          await switchWorkspace(workspaceId);
          setShowSelection(false);
        }}
        onCreateNew={() => router.push("/workspace/settings?create=true")}
      />
    );
  }

  return <>{children}</>;
}

function DashboardContent({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

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
            <Sidebar />

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

              {/* Header */}
              <Header />

              {/* Page Content */}
              <main className="flex-1 overflow-y-auto bg-slate-50 dark:bg-slate-900">
                <div className="container mx-auto p-6">{children}</div>

                {/* Footer */}
                <footer className="border-t border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950 px-6 py-4 mt-8">
                  <div className="container mx-auto flex items-center justify-between text-sm text-slate-600 dark:text-slate-400">
                    <p>© 2025 Kagura Memory Cloud</p>
                    <a
                      href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"}/redoc`}
                      className="hover:text-brand-green-600 transition-colors underline"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      API Documentation
                    </a>
                  </div>
                </footer>
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
