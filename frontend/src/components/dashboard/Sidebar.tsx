"use client";

/**
 * Dashboard Sidebar Navigation
 *
 * Provides role-based navigation links with user menu at bottom.
 * Mobile responsive with hamburger menu.
 * Issue #31: Frontend Redesign Phase 5
 */

import { useState, useEffect, Fragment } from "react";
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
import { hasRole, hasWorkspaceRole, Role } from "@/lib/auth/rbac";
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
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
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
  Gauge,
  ChevronDown,
  AlertTriangle,
  Moon,
  ShieldCheck,
} from "lucide-react";
// Issue #246: ContextSelector removed - use /contexts link instead
import { WorkspaceSwitcher } from "@/components/workspaces/WorkspaceSwitcher";

interface NavItem {
  nameKey: string; // Translation key
  href: string;
  icon: React.ElementType;
  requiredRole?: Role; // System admin role
  requiredWorkspaceRole?: "owner" | "admin" | "member"; // Workspace role (minimum required)
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
        requiredWorkspaceRole: "member", // Issue #398: hide from viewer (read-only role)
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
        requiredWorkspaceRole: "owner", // Issue #389: Owner-only (resource tokens + schema decisions)
      },
      {
        nameKey: "members",
        href: "/workspace/members",
        icon: Users,
        showMemberCount: true,
        requiredWorkspaceRole: "admin", // Issue #398: hide from member/viewer
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
    ],
  },
  {
    titleKey: "settings",
    collapsible: false,
    items: [
      {
        nameKey: "workspaceSettings",
        href: "/workspace/settings/general",
        icon: Sliders,
        requiredWorkspaceRole: "owner",
      },
      {
        nameKey: "externalKeys",
        href: "/workspace/integrations/external-keys",
        icon: KeyRound,
        requiredWorkspaceRole: "owner", // Issue #381: Owner-only (workspace-level secrets)
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
  useEffect(() => {
    setHasExternalKeys(null);
    if (!currentWorkspaceId) return;
    if (currentWorkspaceRole !== "owner") return;

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
  }, [currentWorkspaceId, currentWorkspaceRole]);

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

  const handleLogout = async () => {
    await logout();
  };

  const SidebarContent = () => (
    <>
      {/* Workspace Switcher at Top - Minimal padding */}
      <div className="px-2 py-2">
        <WorkspaceSwitcher />
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-4 py-3 space-y-4 overflow-y-auto">
        {navigationGroups.map((group) => {
          // Filter items based on user role
          const visibleItems = group.items.filter((item) => {
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
                        typography.caption,
                        "uppercase tracking-wider",
                        colors.text.muted,
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
                      typography.caption,
                      "uppercase tracking-wider",
                      colors.text.muted,
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
          <DropdownMenu>
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
              <DropdownMenuLabel className="font-normal">
                <div className="flex flex-col space-y-1">
                  <p className="text-sm font-medium leading-none">
                    {user.name}
                  </p>
                  <p className="text-xs leading-none text-slate-500 dark:text-slate-400">
                    {user.email}
                  </p>
                  {user.role === "admin" && (
                    <p className="text-xs font-medium leading-none text-red-600 dark:text-red-400 mt-1">
                      🛡️ SYSTEM ADMIN
                    </p>
                  )}
                </div>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
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
          "fixed top-4 left-4 z-[100] p-2 rounded-lg md:hidden",
          colors.bg.card,
          colors.border.default,
          "border shadow-lg",
        )}
        aria-label={t("toggleMenu")}
      >
        {isOpen ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
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
