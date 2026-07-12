"use client";

/**
 * Dashboard Sidebar Navigation
 *
 * Provides role-based navigation links with user menu at bottom.
 * Mobile responsive with hamburger menu.
 * Issue #31: Frontend Redesign Phase 5
 */

import { useState, useEffect, useRef, Fragment } from "react";
import Link from "next/link";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { cn as utilCn } from "@/lib/utils/cn";
import {
  cn,
  typography,
  colors,
  spacing,
  transitions,
} from "@/styles/design-tokens";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import {
  hasRole,
  hasWorkspaceRole,
  Role,
  WorkspaceRole,
} from "@/lib/auth/rbac";
import { getContexts } from "@/lib/api/contexts";
import { listExternalAPIKeys } from "@/lib/api/external-keys";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { KaguraLogo } from "@/components/icons/KaguraLogo";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Activity,
  Brain,
  Key,
  Settings,
  Puzzle,
  Database,
  Sliders,
  KeyRound,
  LogOut,
  UserCircle,
  Menu,
  X,
  Users,
  BarChart,
  CreditCard,
  Lock,
  Gauge,
  ChevronDown,
  AlertTriangle,
  Moon,
  ShieldCheck,
  DollarSign,
  Info,
  ExternalLink,
  Plug,
  HardDrive,
} from "lucide-react";
import { apiClient } from "@/lib/api/base";
// Issue #246: ContextSelector removed - use /contexts link instead
import { WorkspaceSwitcher } from "@/components/workspaces/WorkspaceSwitcher";

interface NavItem {
  nameKey: string; // Translation key
  href: string;
  icon: React.ElementType;
  requiredRole?: Role; // System admin role
  requiredWorkspaceRole?: Exclude<WorkspaceRole, WorkspaceRole.Viewer>; // Workspace role (minimum required)
  requiredFeature?: string; // Issue #1145: gate on a GET /system/info features flag
  disabled?: boolean; // Issue #115: Support for "Coming Soon" items
  showMemberCount?: boolean; // Issue #223: Show dynamic member count
  showContextCount?: boolean; // Show dynamic context count
  children?: NavItem[]; // NEW: Support for nested items
}

interface NavGroup {
  titleKey: string; // Translation key (empty string for Context Switcher section)
  items: NavItem[];
  collapsible?: boolean; // Issue #139: Support collapsible sections
  defaultCollapsed?: boolean;
}

// Issue #317: Task-based menu structure for OSS
// Groups: Workspace, Integrations, Settings, Admin
const navigationGroups: NavGroup[] = [
  {
    titleKey: "workspace",
    collapsible: false,
    items: [
      {
        nameKey: "dashboard",
        href: "/workspace/dashboard",
        icon: BarChart,
        requiredWorkspaceRole: WorkspaceRole.Member, // Issue #398: hide from viewer (read-only role)
      },
      {
        nameKey: "contexts",
        href: "/workspace/contexts",
        icon: Brain,
        showContextCount: true,
      },
      {
        nameKey: "resources",
        href: "/workspace/resources",
        icon: Database,
        requiredWorkspaceRole: WorkspaceRole.Owner, // Issue #389: Owner-only (resource tokens + schema decisions)
      },
      {
        // Issue #955: workspace-scoped file objects (R2 storage). Backend
        // lists/downloads at viewer+, so the entry is visible to every
        // workspace role; delete is gated to member+ inside the page.
        nameKey: "storage",
        href: "/workspace/storage",
        icon: HardDrive,
      },
      {
        // Issue #1134: owner/admin secret store console. Lives in the Workspace
        // group (not Settings) because it is a workspace-scoped store, like
        // resources/storage — not a configuration surface. The list APIs
        // (GET /config/secrets, /pubkeys) are owner/admin and approve/revoke of
        // recipient keys is owner-only inside the page, so gate the nav to admin+
        // so member/viewer don't see a link that would 403 on click.
        nameKey: "secrets",
        href: "/workspace/secrets",
        icon: Lock,
        requiredWorkspaceRole: WorkspaceRole.Admin,
      },
      {
        nameKey: "members",
        href: "/workspace/members",
        icon: Users,
        showMemberCount: true,
        requiredWorkspaceRole: WorkspaceRole.Admin, // Issue #398: hide from member/viewer
      },
      {
        // Issue #526: workspace-scoped sleep reports view.
        // Backend gates owner/admin; sidebar mirrors so member/viewer
        // don't see a nav entry that would 403 on click.
        nameKey: "sleepReports",
        href: "/workspace/sleep-reports",
        icon: Moon,
        requiredWorkspaceRole: WorkspaceRole.Admin,
      },
      {
        // Issue #473: workspace-scoped cost dashboard.
        // Backend gates owner/admin via check_workspace_admin (#472);
        // mirror that here so member/viewer don't see a nav entry that
        // would 403 on click.
        // Issue #1167: part of the BYOK surface — hidden when ENABLE_BYOK
        // is off (the backing API 404s). Reporting entries stay last in
        // the group: sleepReports, then cost.
        nameKey: "cost",
        href: "/workspace/cost",
        icon: DollarSign,
        requiredWorkspaceRole: WorkspaceRole.Admin,
        requiredFeature: "byok",
      },
    ],
  },
  {
    titleKey: "integrations",
    collapsible: false,
    items: [
      {
        nameKey: "apiKeys",
        href: "/workspace/integrations/credentials?tab=api-keys",
        icon: Key,
      },
      {
        nameKey: "oauthApps",
        href: "/workspace/integrations/credentials?tab=oauth-apps",
        icon: Puzzle,
      },
      {
        nameKey: "connectors",
        href: "/workspace/integrations/connectors",
        icon: Plug,
        requiredWorkspaceRole: WorkspaceRole.Admin,
      },
    ],
  },
  {
    titleKey: "settings",
    collapsible: false,
    items: [
      {
        // Issue #1121: owner plan view + billing handoff. The page's data API
        // (getWorkspacePlan) is owner-only (#246), so gate the nav entry to
        // owner too — mirrors the other settings items and avoids a link that
        // would 403 on click for member/viewer.
        nameKey: "plan",
        href: "/workspace/settings/plan",
        icon: CreditCard,
        requiredWorkspaceRole: WorkspaceRole.Owner,
        // Issue #1145: also gated behind the backend ENABLE_PLAN_PAGE flag
        // (default-off in OSS) — hidden until /system/info confirms plan_page.
        requiredFeature: "plan_page",
      },
      {
        nameKey: "workspaceSettings",
        href: "/workspace/settings/general",
        icon: Sliders,
        requiredWorkspaceRole: WorkspaceRole.Owner,
      },
      {
        nameKey: "externalKeys",
        href: "/workspace/integrations/external-keys",
        icon: KeyRound,
        requiredWorkspaceRole: WorkspaceRole.Owner, // Issue #381: Owner-only (workspace-level secrets)
        // Issue #1167: the BYOK console itself — hidden when ENABLE_BYOK is
        // off (its API returns 404).
        requiredFeature: "byok",
      },
    ],
  },
  {
    titleKey: "admin",
    items: [
      {
        nameKey: "users",
        href: "/admin/users",
        icon: Users,
        requiredRole: Role.ADMIN,
      },
      {
        nameKey: "quotas",
        href: "/admin/plans",
        icon: Gauge,
        requiredRole: Role.ADMIN,
      },
      {
        nameKey: "neuralConfig",
        href: "/admin/neural-config",
        icon: Brain,
        requiredRole: Role.ADMIN,
      },
      {
        nameKey: "sleepReports",
        href: "/admin/sleep-reports",
        icon: Moon,
        requiredRole: Role.ADMIN,
      },
      {
        // Issue #1211: consolidated memory-health report
        nameKey: "memoryHealth",
        href: "/admin/memory-health",
        icon: Activity,
        requiredRole: Role.ADMIN,
      },
      {
        // Issue #473: cost dashboard
        nameKey: "cost",
        href: "/admin/cost",
        icon: DollarSign,
        requiredRole: Role.ADMIN,
      },
      {
        nameKey: "environment",
        href: "/admin/environment",
        icon: Settings,
        requiredRole: Role.ADMIN,
      },
      {
        // Issue #358: admin-configurable signup gate
        nameKey: "signupGate",
        href: "/admin/signup-gate",
        icon: ShieldCheck,
        requiredRole: Role.ADMIN,
      },
    ],
  },
];

export function Sidebar() {
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  // Issue #1145: backend feature flags (e.g. plan_page). null while loading →
  // feature-gated items stay hidden (default-off).
  const systemFeatures = useSystemFeatures();
  const [isOpen, setIsOpen] = useState(false);
  const [contextCount, setContextCount] = useState<number | null>(null);
  const [hasExternalKeys, setHasExternalKeys] = useState<boolean | null>(null);
  const t = useTranslations("sidebar");
  const tNav = useTranslations("navigation");

  const [collapsedSections, setCollapsedSections] = useState<
    Record<string, boolean>
  >(() => {
    // Merge localStorage with current menu structure to handle menu changes
    const initial: Record<string, boolean> = {};

    // Load saved state
    let saved: Record<string, boolean> = {};
    if (typeof window !== "undefined") {
      const savedStr = localStorage.getItem("sidebar-collapsed-sections");
      if (savedStr) {
        try {
          saved = JSON.parse(savedStr);
        } catch (e) {
          console.error("Failed to parse saved collapsed sections:", e);
        }
      }
    }

    // Merge with current structure (handles menu changes gracefully)
    navigationGroups.forEach((group) => {
      if (group.collapsible) {
        initial[group.titleKey] =
          saved[group.titleKey] ?? (group.defaultCollapsed || false);
      }
      // Initialize nested items
      group.items.forEach((item) => {
        if (item.children) {
          const key = `item-${item.nameKey}`;
          initial[key] = saved[key] ?? false; // Default: expanded
        }
      });
    });

    return initial;
  });
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const currentTab = searchParams.get("tab");
  const router = useRouter();
  const { user, logout } = useAuth();

  // Load context count on mount / workspace switch.
  // Reset to null on each run so the warning icon does not show a stale
  // "0 contexts" value from the previous workspace while the new fetch is
  // in flight. Cancellation guard prevents a late fetch from clobbering a
  // newer workspace's state (same pattern as hasExternalKeys below).
  useEffect(() => {
    setContextCount(null);
    if (!currentWorkspaceId) return;

    let cancelled = false;
    getContexts()
      .then((data) => {
        if (!cancelled) setContextCount(data.contexts.length);
      })
      .catch((error) => {
        if (process.env.NODE_ENV === "development") {
          console.error("Failed to load context count for sidebar:", error);
        }
        if (!cancelled) setContextCount(null);
        // Note: Error not shown in UI to keep sidebar clean
        // Users can see detailed error on /workspace/contexts page
      });

    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId]);

  // Load external key status (with race condition guard).
  // Issue #381: external keys are owner-only. Non-owners would get 403 — skip the
  // fetch entirely for them to avoid noisy auth logs / avoidable network traffic.
  // The AlertTriangle indicator on the nav item is owner-only too, so this also
  // keeps the `hasExternalKeys=null` path unreachable for non-owners.
  const currentWorkspaceRole = currentWorkspace?.current_user_role;
  const byokEnabled = systemFeatures?.byok === true;
  useEffect(() => {
    setHasExternalKeys(null);
    if (!currentWorkspaceId) return;
    if (currentWorkspaceRole !== "owner") return;
    // Issue #1167: with BYOK off the external-keys API 404s and the nav entry
    // is hidden anyway — skip the probe (also while features are loading).
    if (!byokEnabled) return;

    let cancelled = false;
    listExternalAPIKeys()
      .then((keys) => {
        if (!cancelled) setHasExternalKeys(keys.length > 0);
      })
      .catch(() => {
        if (!cancelled) setHasExternalKeys(null);
      });

    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId, currentWorkspaceRole, byokEnabled]);

  // Sync collapse state across browser tabs
  useEffect(() => {
    const handleStorageChange = (e: StorageEvent) => {
      if (e.key === "sidebar-collapsed-sections" && e.newValue) {
        try {
          setCollapsedSections(JSON.parse(e.newValue));
        } catch (err) {
          console.error("Failed to parse storage event:", err);
        }
      }
    };

    window.addEventListener("storage", handleStorageChange);
    return () => window.removeEventListener("storage", handleStorageChange);
  }, []);

  const toggleSection = (titleKey: string) => {
    setCollapsedSections((prev) => {
      const newState = {
        ...prev,
        [titleKey]: !prev[titleKey],
      };

      // Save to localStorage
      if (typeof window !== "undefined") {
        localStorage.setItem(
          "sidebar-collapsed-sections",
          JSON.stringify(newState),
        );
      }

      return newState;
    });
  };

  // Helper to get translated item name with optional counts
  const getItemName = (item: NavItem): string => {
    // Skip separator items
    if (item.nameKey.startsWith("---")) {
      return "";
    }
    if (
      item.showMemberCount &&
      currentWorkspace?.member_count &&
      currentWorkspace.member_count >= 2
    ) {
      return t("membersWithCount", { count: currentWorkspace.member_count });
    }
    if (item.showContextCount && contextCount !== null && contextCount > 0) {
      return t("contextsWithCount", { count: contextCount });
    }
    // NOTE: 'as any' is used because next-intl's useTranslations() doesn't support dynamic keys
    // from navigationGroups. A proper fix would require defining a union type of all possible
    // translation keys, which would be a large refactoring.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return t(item.nameKey as any);
  };

  const getUserInitials = () => {
    if (!user?.name) return "U";
    const names = user.name.split(" ");
    if (names.length >= 2) {
      return `${names[0][0]}${names[1][0]}`.toUpperCase();
    }
    return user.name.substring(0, 2).toUpperCase();
  };

  // App version is fetched from the backend (GET /api/v1/system/info) the first
  // time the user menu opens, then cached for the session. The backend
  // `constants.py` APP_VERSION is the single source of truth — no hardcoded
  // frontend version (which drifts from the backend during blue/green deploys).
  // `—` placeholder is shown before load and on failure (non-blocking).
  // (Language switching lives on the profile page — see profile/page.tsx.)
  const [systemVersion, setSystemVersion] = useState<string | null>(null);
  const versionFetchedRef = useRef(false);

  const handleUserMenuOpenChange = (open: boolean) => {
    if (!open || versionFetchedRef.current) return;
    versionFetchedRef.current = true; // fetch at most once per session
    apiClient
      .get<{ version: string }>("/api/v1/system/info")
      .then((info) => setSystemVersion(info.version))
      .catch(() => {
        // Leave systemVersion null → "v—" placeholder. Non-blocking by design.
      });
  };

  const handleLogout = async () => {
    await logout();
  };

  const SidebarContent = () => (
    <>
      {/* Kagura Logo (formerly in Header) */}
      <Link
        href="/workspace/dashboard"
        aria-label={t("kaguraLogoAria")}
        className={utilCn(
          "flex items-center justify-center py-2 hover:opacity-80",
          transitions.default,
        )}
        onClick={() => setIsOpen(false)}
      >
        <KaguraLogo className="h-6 w-auto" variant="image" surface="auto" />
      </Link>

      {/* Workspace Switcher at Top - Minimal padding */}
      <div className="px-2 pt-0 pb-1">
        <WorkspaceSwitcher />
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-4 py-3 space-y-4 overflow-y-auto">
        {navigationGroups.map((group) => {
          // Filter items based on user role
          const visibleItems = group.items.filter((item) => {
            // Issue #1145: gate behind a backend feature flag. Default-off:
            // hidden while features load (null) and unless the flag is true.
            if (
              item.requiredFeature &&
              !systemFeatures?.[item.requiredFeature]
            ) {
              return false;
            }
            // Check system admin role
            if (item.requiredRole && !hasRole(user, item.requiredRole)) {
              return false;
            }
            // Check workspace role (Issue #59: uses hierarchy-based check)
            if (item.requiredWorkspaceRole) {
              return hasWorkspaceRole(
                currentWorkspace?.current_user_role,
                item.requiredWorkspaceRole,
              );
            }
            return true;
          });

          // Hide group if no items are visible (keep Context Switcher group)
          if (visibleItems.length === 0 && group.titleKey !== "") return null;

          const isCollapsed =
            group.collapsible && collapsedSections[group.titleKey];
          // NOTE: 'as any' is used for dynamic translation keys from navigationGroups
          // A proper fix would require union type of all keys (large refactoring)
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const groupTitle = group.titleKey ? t(group.titleKey as any) : "";

          return (
            <Fragment key={group.titleKey}>
              {/* Issue #246: ContextSelector removed - use /contexts link in Workspaces */}

              <div key={group.titleKey}>
                {group.collapsible ? (
                  <button
                    onClick={() => toggleSection(group.titleKey)}
                    className={utilCn(
                      "w-full flex items-center justify-between px-3 mb-2",
                      "hover:bg-slate-100 dark:hover:bg-slate-800 rounded-md py-1",
                      transitions.default,
                    )}
                  >
                    <h3
                      className={cn(
                        // typography.caption already carries dark:text-slate-400
                        // (WCAG-AA on the dark sidebar). Layering colors.text.muted
                        // (dark:text-slate-500) on top loses contrast in dark mode
                        // — keep caption's color only (#840 a11y fix).
                        typography.caption,
                        "uppercase tracking-wider",
                      )}
                    >
                      {groupTitle}
                    </h3>
                    <ChevronDown
                      className={utilCn(
                        "h-4 w-4",
                        colors.text.muted,
                        transitions.default,
                        isCollapsed ? "-rotate-90" : "rotate-0",
                      )}
                    />
                  </button>
                ) : group.titleKey ? (
                  <h3
                    className={cn(
                      "px-3 mb-2",
                      // See the collapsible h3 above — caption's dark:text-slate-400
                      // is WCAG-AA on the dark sidebar; colors.text.muted would
                      // drop it to slate-500 and fail dark-mode contrast (#840).
                      typography.caption,
                      "uppercase tracking-wider",
                    )}
                  >
                    {groupTitle}
                  </h3>
                ) : null}
                <div
                  className={utilCn(
                    "space-y-1",
                    transitions.default,
                    isCollapsed ? "hidden" : "block",
                  )}
                >
                  {visibleItems.map((item) => {
                    const Icon = item.icon;
                    const itemName = getItemName(item);

                    // Issue #217: Render separator for owner section
                    if (item.nameKey.startsWith("---")) {
                      const labelKey = item.nameKey.replace(/---/g, "").trim();
                      return (
                        <div
                          key={item.nameKey}
                          className="flex items-center gap-2 px-3 py-1 mt-2 mb-1"
                        >
                          <div className="flex-1 border-t border-slate-300/50 dark:border-slate-600/50" />
                          <span className="text-[10px] uppercase tracking-wider text-slate-400 dark:text-slate-500">
                            {t(labelKey as any)}
                          </span>
                          <div className="flex-1 border-t border-slate-300/50 dark:border-slate-600/50" />
                        </div>
                      );
                    }

                    // NEW: Handle nested items (collapsible parent with children)
                    if (item.children) {
                      const childKey = `item-${item.nameKey}`;
                      const isChildrenCollapsed = collapsedSections[childKey];

                      // Filter visible children based on roles
                      const visibleChildren = item.children.filter((child) => {
                        if (
                          child.requiredRole &&
                          !hasRole(user, child.requiredRole)
                        ) {
                          return false;
                        }
                        if (child.requiredWorkspaceRole) {
                          return hasWorkspaceRole(
                            currentWorkspace?.current_user_role,
                            child.requiredWorkspaceRole,
                          );
                        }
                        return true;
                      });

                      // Don't render parent if no children visible
                      if (visibleChildren.length === 0) return null;

                      return (
                        <div key={item.nameKey}>
                          {/* Parent toggle button */}
                          <button
                            onClick={() => toggleSection(childKey)}
                            className={utilCn(
                              "w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium",
                              transitions.default,
                              colors.text.secondary,
                              colors.bg.hover,
                            )}
                          >
                            <Icon className="h-5 w-5 flex-shrink-0" />
                            <span className="flex-1 text-left">{itemName}</span>
                            <ChevronDown
                              className={utilCn(
                                "h-4 w-4",
                                transitions.default,
                                isChildrenCollapsed ? "-rotate-90" : "rotate-0",
                              )}
                            />
                          </button>

                          {/* Children */}
                          {!isChildrenCollapsed && (
                            <div className="ml-8 mt-1 space-y-1">
                              {visibleChildren.map((child) => {
                                const ChildIcon = child.icon;
                                const childName = t(child.nameKey as any);
                                // Match child item: exact match OR prefix match for nested routes
                                const childActive =
                                  pathname === child.href ||
                                  (child.href !== "#" &&
                                    pathname.startsWith(child.href + "/"));

                                if (child.disabled) {
                                  return (
                                    <div
                                      key={child.nameKey}
                                      className={utilCn(
                                        "flex items-center gap-3 px-3 py-2 rounded-lg text-sm",
                                        "opacity-50 cursor-not-allowed",
                                        colors.text.muted,
                                      )}
                                      title={t("comingSoon")}
                                    >
                                      <ChildIcon className="h-4 w-4 flex-shrink-0" />
                                      <span className="flex-1">
                                        {childName}
                                      </span>
                                      <span className="text-xs bg-slate-200 dark:bg-slate-700 px-1.5 py-0.5 rounded">
                                        {t("soon")}
                                      </span>
                                    </div>
                                  );
                                }

                                return (
                                  <Link
                                    key={child.nameKey}
                                    href={child.href}
                                    onClick={() => setIsOpen(false)}
                                    className={utilCn(
                                      "flex items-center gap-3 px-3 py-2 rounded-lg text-sm",
                                      transitions.default,
                                      childActive
                                        ? `${colors.bg.selected} ${colors.text.primary}`
                                        : `${colors.text.secondary} ${colors.bg.hover}`,
                                    )}
                                  >
                                    <ChildIcon className="h-4 w-4 flex-shrink-0" />
                                    {childName}
                                  </Link>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      );
                    }

                    // Regular items (no children)
                    // Handle hrefs with query params (e.g. /credentials?tab=api-keys)
                    const [itemPath, itemQuery] = item.href.split("?");
                    const itemTab = itemQuery
                      ? new URLSearchParams(itemQuery).get("tab")
                      : null;
                    const isActive = itemTab
                      ? pathname === itemPath && currentTab === itemTab
                      : pathname === item.href ||
                        (item.href !== "/dashboard" &&
                          pathname.startsWith(item.href + "/"));

                    // Issue #115: Disabled items show as "Coming Soon"
                    if (item.disabled) {
                      return (
                        <div
                          key={item.nameKey}
                          className={utilCn(
                            "flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium",
                            "opacity-50 cursor-not-allowed",
                            colors.text.muted,
                          )}
                          title={t("comingSoon")}
                        >
                          <Icon className="h-5 w-5 flex-shrink-0" />
                          <span className="flex-1">{itemName}</span>
                          <span className="text-xs bg-slate-200 dark:bg-slate-700 px-1.5 py-0.5 rounded">
                            {t("soon")}
                          </span>
                        </div>
                      );
                    }

                    return (
                      <Link
                        key={item.nameKey}
                        href={item.href}
                        onClick={() => setIsOpen(false)}
                        className={utilCn(
                          "flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium",
                          transitions.default,
                          isActive
                            ? `${colors.bg.selected} ${colors.text.primary}`
                            : `${colors.text.secondary} ${colors.bg.hover}`,
                        )}
                      >
                        <Icon className="h-5 w-5 flex-shrink-0" />
                        <span className="flex-1">{itemName}</span>
                        {item.nameKey === "externalKeys" &&
                          hasExternalKeys === false && (
                            <span
                              title={t("noExternalKeys", {
                                default: "No API keys configured",
                              })}
                            >
                              <AlertTriangle
                                className="h-4 w-4 text-amber-500 flex-shrink-0"
                                aria-label={t("noExternalKeys", {
                                  default: "No API keys configured",
                                })}
                              />
                            </span>
                          )}
                        {/* null = loading-or-error (stay quiet; errors surface on /workspace/contexts) */}
                        {item.nameKey === "contexts" && contextCount === 0 && (
                          <span title={t("noContexts")}>
                            <AlertTriangle
                              className="h-4 w-4 text-yellow-500 flex-shrink-0"
                              aria-label={t("noContexts")}
                            />
                          </span>
                        )}
                      </Link>
                    );
                  })}
                </div>
              </div>
            </Fragment>
          );
        })}
      </nav>

      {/* User Menu at Bottom */}
      <div className={cn("px-4 py-3", "border-t", colors.border.default)}>
        {user && (
          <DropdownMenu onOpenChange={handleUserMenuOpenChange}>
            <DropdownMenuTrigger asChild>
              <button
                className={cn(
                  "w-full flex items-center gap-3 px-3 py-2 rounded-lg",
                  colors.bg.hover,
                  transitions.default,
                )}
              >
                <Avatar className="h-10 w-10">
                  <AvatarImage src={user.picture} alt={user.name} />
                  <AvatarFallback className="bg-gradient-to-br from-brand-green-600 to-emerald-600 text-white">
                    {getUserInitials()}
                  </AvatarFallback>
                </Avatar>
                <div className="flex-1 text-left overflow-hidden">
                  <p
                    className={cn(
                      typography.bodySmall,
                      "font-semibold truncate",
                    )}
                  >
                    {user.name}
                  </p>
                  {user.role === "admin" && (
                    <p
                      className={cn(
                        typography.caption,
                        "text-red-600 dark:text-red-400 font-medium",
                      )}
                    >
                      {t("systemAdmin")}
                    </p>
                  )}
                </div>
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent className="w-56" align="end" side="top">
              {/* Profile Settings */}
              <DropdownMenuItem
                onClick={() => {
                  router.push("/profile");
                  setIsOpen(false);
                }}
              >
                <UserCircle className="mr-2 h-4 w-4" />
                <span>{t("profileSettings")}</span>
              </DropdownMenuItem>

              <DropdownMenuSeparator />

              {/* View Details submenu */}
              <DropdownMenuSub>
                <DropdownMenuSubTrigger>
                  <Info className="mr-2 h-4 w-4" />
                  <span>{t("viewDetails")}</span>
                </DropdownMenuSubTrigger>
                <DropdownMenuSubContent className="w-64">
                  <DropdownMenuItem asChild>
                    <a
                      href="https://www.kagura-ai.com/terms"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center cursor-pointer"
                    >
                      <span className="flex-1">{t("termsOfService")}</span>
                      <ExternalLink className="ml-2 h-3.5 w-3.5 opacity-60" />
                    </a>
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild>
                    <a
                      href="https://www.kagura-ai.com/privacy"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center cursor-pointer"
                    >
                      <span className="flex-1">{t("privacyPolicy")}</span>
                      <ExternalLink className="ml-2 h-3.5 w-3.5 opacity-60" />
                    </a>
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild>
                    <a
                      href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"}/redoc`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center cursor-pointer"
                    >
                      <span className="flex-1">{t("apiDocumentation")}</span>
                      <ExternalLink className="ml-2 h-3.5 w-3.5 opacity-60" />
                    </a>
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild>
                    <a
                      href="https://www.kagura-ai.com"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center cursor-pointer"
                    >
                      <span className="flex-1">{t("aboutKagura")}</span>
                      <ExternalLink className="ml-2 h-3.5 w-3.5 opacity-60" />
                    </a>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuLabel className="text-[10px] font-normal text-slate-500 dark:text-slate-400">
                    {t("copyright", { year: new Date().getFullYear() })}
                    {" · "}
                    {t("version", { version: systemVersion ?? "—" })}
                  </DropdownMenuLabel>
                </DropdownMenuSubContent>
              </DropdownMenuSub>

              <DropdownMenuSeparator />

              {/* Log Out */}
              <DropdownMenuItem
                onClick={handleLogout}
                className="text-red-600 dark:text-red-400"
              >
                <LogOut className="mr-2 h-4 w-4" />
                <span>{t("logOut")}</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </>
  );

  return (
    <>
      {/* Mobile: Hamburger Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={cn(
          "fixed top-2 left-2 z-[100] p-1.5 rounded-lg md:hidden",
          "text-slate-700 dark:text-slate-200",
          colors.bg.card,
          colors.border.default,
          "border shadow-lg",
        )}
        aria-label={t("toggleMenu")}
      >
        {isOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
      </button>

      {/* Mobile: Overlay */}
      {isOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-[60] md:hidden"
          onClick={() => setIsOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={utilCn(
          "fixed inset-y-0 left-0 z-[70] w-64 flex flex-col",
          "md:relative md:z-0",
          colors.bg.card,
          colors.border.default,
          "border-r",
          transitions.all,
          isOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0",
        )}
      >
        <SidebarContent />
      </aside>
    </>
  );
}
