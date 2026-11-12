"use client";

/**
 * Dashboard Sidebar Navigation
 *
 * Provides role-based navigation links with user menu at bottom.
 * Mobile responsive with hamburger menu.
 * Issue #31: Frontend Redesign Phase 5
 */

import { useState, useEffect, useRef, useSyncExternalStore, Fragment } from "react";
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
import { checkOpenAIKeyStatus } from "@/lib/api/workspaces";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { sidebarNavigationGroups as navigationGroups, type NavItem } from "@/lib/navigation/featureNavigation";
import { useBetaInvites } from "@/hooks/useBetaInvites";
import {
  hasRole,
  isSystemAdmin,
  hasWorkspaceRole,
  WorkspaceRole,
} from "@/lib/auth/rbac";
import type { LogoutScope } from "@/lib/auth/auth";
import { getContexts } from "@/lib/api/contexts";
import { useAccountSwitcher } from "@/hooks/useAccountSwitcher";
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

import { apiClient } from "@/lib/api/base";
import { LogOut, UserCircle, Menu, ChevronDown, AlertTriangle, Info, ExternalLink, UserPlus, MailPlus, Check } from "lucide-react";
// Issue #246: ContextSelector removed - use /contexts link instead
import { WorkspaceSwitcher } from "@/components/workspaces/WorkspaceSwitcher";
import { InviteFriendCard } from "@/components/beta-invites/InviteFriendCard";
import { BetaInviteDialog } from "@/components/beta-invites/BetaInviteDialog";

const mobileQuery = "(max-width: 767px)";
function watchViewport(notify: () => void) {
  const query = window.matchMedia(mobileQuery);
  query.addEventListener("change", notify);
  return () => query.removeEventListener("change", notify);
}
const isMobileViewport = () => window.matchMedia(mobileQuery).matches;

export function Sidebar() {
  const isMobile = useSyncExternalStore(watchViewport, isMobileViewport, () => false);
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  // Issue #1145: backend feature flags (e.g. plan_page). null while loading →
  // feature-gated items stay hidden (default-off).
  const systemFeatures = useSystemFeatures();
  const [isOpen, setIsOpen] = useState(false);
  const [contextCount, setContextCount] = useState<number | null>(null);
  // #1495: whether embeddings WORK, not whether this workspace owns a key.
  // Counting key rows warned every workspace served by the platform credential
  // that it was broken and had to go buy an API key.
  const [canEmbed, setCanEmbed] = useState<boolean | null>(null);
  const t = useTranslations("sidebar");
  const tNav = useTranslations("navigation");
  const tInvite = useTranslations("betaInvites");

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
  // newer workspace's state (same pattern as canEmbed below).
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
  // keeps the `canEmbed=null` path unreachable for non-owners.
  const currentWorkspaceRole = currentWorkspace?.current_user_role;
  const byokEnabled = systemFeatures?.byok === true;
  useEffect(() => {
    setCanEmbed(null);
    if (!currentWorkspaceId) return;
    if (currentWorkspaceRole !== "owner") return;
    // Issue #1167: the key-status route is behind `require_byok_enabled` and
    // 404s with BYOK off — skip the probe (also while features are loading).
    // #1616: the nav entry may still show then (stored keys), without the
    // warning icon: this probe stays off, so `canEmbed` stays null.
    if (!byokEnabled) return;

    let cancelled = false;
    // #1495: ask the server whether embedding is available, rather than
    // re-deriving it from a key count the server does not use that way.
    checkOpenAIKeyStatus(currentWorkspaceId)
      .then((status) => {
        // `?? null` — a server predating the field has told us nothing, which
        // is not the same as telling us embedding is unavailable. Unknown must
        // stay quiet; asserting a problem we have not confirmed IS this bug.
        if (!cancelled) setCanEmbed(status.embedding_available ?? null);
      })
      .catch(() => {
        // null = unknown; stay quiet rather than assert a problem we could not
        // confirm. A false warning is worse than none — that is this bug.
        if (!cancelled) setCanEmbed(null);
      });

    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId, currentWorkspaceRole, byokEnabled]);

  // Issue #1582: beta invites. Off (or an older backend without the flag) →
  // no card, no menu entry, no dialog, and the hook makes no request. The one
  // hook instance feeds the card, the menu counter and the dialog alike.
  const betaInvitesEnabled = systemFeatures?.beta_invites === true;
  const betaInvites = useBetaInvites();
  const [inviteDialogOpen, setInviteDialogOpen] = useState(false);
  const openInviteDialog = () => {
    setInviteDialogOpen(true);
    // The mobile drawer (z-[70]) would sit on top of the dialog (z-50).
    setIsOpen(false);
  };

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
      hasWorkspaceRole(currentWorkspaceRole, WorkspaceRole.Admin) &&
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

  // Accounts signed in on THIS browser session (#1488). The decisions live in
  // the hook; this component only renders them.
  const {
    accounts,
    addableProviders,
    switchingTo,
    refresh: refreshAccounts,
    switchTo: switchToAccount,
    addAccount: handleAddAccount,
  } = useAccountSwitcher();

  const handleUserMenuOpenChange = (open: boolean) => {
    if (!open) return;

    // Re-read on every open: another tab may have added or switched an
    // account, and a stale list would offer a switch the server rejects.
    void refreshAccounts();

    if (versionFetchedRef.current) return;
    versionFetchedRef.current = true; // fetch at most once per session
    apiClient
      .get<{ version: string }>("/api/v1/system/info")
      .then((info) => setSystemVersion(info.version))
      .catch(() => {
        // Leave systemVersion null → "v—" placeholder. Non-blocking by design.
      });
  };

  // Defaults to "all" so every pre-#1488 call site is unchanged.
  const handleLogout = async (scope: LogoutScope = "all") => {
    await logout(scope);
  };

  // An ELEMENT, not a component (#1488 Phase 4).
  //
  // This used to be `const SidebarContent = () => (...)` — a component defined
  // inside `Sidebar`. Every render of `Sidebar` created a new function, and a
  // new function is a new component TYPE to React, so the entire subtree was
  // unmounted and remounted on any state change here. That destroys the state
  // React does not own, which includes the Radix `DropdownMenu`'s uncontrolled
  // open flag: the user menu closed itself.
  //
  // Latent before #1488 (the version fetch on first open did it once), the
  // account switcher made it fire on EVERY open — `handleUserMenuOpenChange`
  // calls `refreshAccounts()`, whose `setAccounts` re-renders `Sidebar` the
  // moment GET /auth/accounts resolves, closing the menu ~200ms after it
  // opened. The Phase 3 switcher was effectively unreachable.
  //
  // Holding the JSX in a variable fixes it at the root: an element is
  // reconciled by position and type like any other child, so nothing remounts.
  const sidebarContent = (
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
            if (item.requiredRole && !hasRole(user, item.requiredRole)) return false;
            // Deployment-disabled features stay hidden until every required flag is enabled.
            const requiredFeatures =
              typeof item.requiredFeature === "string"
                ? [item.requiredFeature]
                : (item.requiredFeature ?? []);
            if (
              requiredFeatures.some((flag) => !systemFeatures?.[flag])
            ) {
              return false;
            }
            // Workspace roles and plan limits are explained at the destination.
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

                      const visibleChildren = item.children;

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
                    const isActive = item.activePaths?.some(path => pathname === path || pathname.startsWith(path + "/")) || (itemTab
                      ? pathname === itemPath && currentTab === itemTab
                      : pathname === item.href ||
                        (!item.exact && item.href !== "/dashboard" &&
                          pathname.startsWith(item.href + "/")));

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
                        aria-current={isActive ? "page" : undefined}
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
                          canEmbed === false && (
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

      {/* Invite a friend (#1582) — renders nothing until the summary is known */}
      {betaInvitesEnabled && (
        <InviteFriendCard
          summary={betaInvites.summary}
          onOpen={openInviteDialog}
        />
      )}

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
                  {isSystemAdmin(user, systemFeatures) && (
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
            <DropdownMenuContent className="w-64" align="end" side="top">
              {/* Account switcher (#1488).
                  Rows render only when this session actually holds more than
                  one account — with a single account there is nothing to switch
                  between and the trigger above already shows who you are, so
                  the rows would be pure noise. "Add another account" is always
                  offered, since that is how a second one gets here. */}
              {accounts.length > 1 && (
                <>
                  <DropdownMenuLabel className="text-xs font-normal text-slate-500 dark:text-slate-400">
                    {t("signedInAccounts")}
                  </DropdownMenuLabel>
                  {accounts.map((account) => (
                    <DropdownMenuItem
                      key={account.user_id}
                      disabled={account.is_active || switchingTo !== null}
                      onClick={() => {
                        if (!account.is_active) {
                          void switchToAccount(account.user_id);
                        }
                      }}
                      className="gap-2"
                    >
                      <Avatar className="h-6 w-6 shrink-0">
                        <AvatarImage
                          src={account.picture ?? undefined}
                          alt={account.name ?? ""}
                        />
                        <AvatarFallback className="text-[10px]">
                          {(account.name ?? account.email ?? "?")
                            .substring(0, 2)
                            .toUpperCase()}
                        </AvatarFallback>
                      </Avatar>
                      <div className="flex-1 overflow-hidden">
                        <p className="truncate text-sm">
                          {account.name ?? account.email}
                        </p>
                        {account.name && account.email && (
                          <p className="truncate text-xs text-slate-500 dark:text-slate-400">
                            {account.email}
                          </p>
                        )}
                      </div>
                      {account.is_active && (
                        <Check
                          className="h-4 w-4 shrink-0 text-brand-green-600"
                          aria-label={t("activeAccount")}
                        />
                      )}
                    </DropdownMenuItem>
                  ))}
                  <DropdownMenuSeparator />
                </>
              )}

              {/* Add another account.
                  Offered only for providers this deployment actually has. The
                  item is a full-page navigation to the API's OAuth login, and
                  on an instance without that provider configured the endpoint
                  answers 500 with raw JSON — which would throw the user out of
                  the SPA onto an error page with only the back button to
                  recover. The OSS default is password login with no OAuth at
                  all, so "assume Google" is wrong by default, not just at the
                  edges. */}
              {addableProviders.length === 1 && (
                <>
                  <DropdownMenuItem
                    onClick={() => handleAddAccount(addableProviders[0])}
                  >
                    <UserPlus className="mr-2 h-4 w-4" />
                    <span>{t("addAnotherAccount")}</span>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                </>
              )}
              {addableProviders.length > 1 && (
                <>
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger>
                      <UserPlus className="mr-2 h-4 w-4" />
                      <span>{t("addAnotherAccount")}</span>
                    </DropdownMenuSubTrigger>
                    <DropdownMenuSubContent>
                      {addableProviders.map((provider) => (
                        <DropdownMenuItem
                          key={provider}
                          onClick={() => handleAddAccount(provider)}
                        >
                          {t(
                            provider === "google"
                              ? "addWithGoogle"
                              : "addWithGitHub",
                          )}
                        </DropdownMenuItem>
                      ))}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                  <DropdownMenuSeparator />
                </>
              )}

              {/* Settings (/profile): profile, preferences, security, linked accounts */}
              <DropdownMenuItem
                onClick={() => {
                  router.push("/profile");
                  setIsOpen(false);
                }}
              >
                <UserCircle className="mr-2 h-4 w-4" />
                <span>{t("profileSettings")}</span>
              </DropdownMenuItem>

              {/* Invite a friend (#1582) — permanent, so the dialog stays
                  reachable at the cap and after the card was dismissed. No
                  counter while the summary loads, no denominator for an
                  admin (quota null = unlimited). */}
              {betaInvitesEnabled && (
                <DropdownMenuItem onClick={openInviteDialog}>
                  <MailPlus className="mr-2 h-4 w-4" />
                  <span>
                    {betaInvites.summary && betaInvites.summary.quota !== null
                      ? tInvite("menuEntryWithCount", {
                          used: betaInvites.summary.used,
                          quota: betaInvites.summary.quota,
                        })
                      : tInvite("menuEntry")}
                  </span>
                </DropdownMenuItem>
              )}

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

              {/* Sign out (#1488 Phase 4).
                  With one account "log out" is unambiguous, so it stays a
                  single item reading exactly as before. With more than one it
                  splits, because the two acts differ: signing out of the
                  active account leaves the others usable, while signing out of
                  all ends the session. Offering only the wide one would evict
                  accounts the user never mentioned; offering only the narrow
                  one would leave them signed in on a shared machine. */}
              {accounts.length > 1 ? (
                <>
                  <DropdownMenuItem
                    onClick={() => handleLogout("current")}
                    className="text-red-600 dark:text-red-400"
                  >
                    <LogOut className="mr-2 h-4 w-4 shrink-0" />
                    <span className="truncate">
                      {t("signOutThisAccount", {
                        name: user.email || user.name,
                      })}
                    </span>
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={() => handleLogout("all")}
                    className="text-red-600 dark:text-red-400"
                  >
                    <LogOut className="mr-2 h-4 w-4 shrink-0" />
                    <span>{t("signOutAllAccounts")}</span>
                  </DropdownMenuItem>
                </>
              ) : (
                <DropdownMenuItem
                  onClick={() => handleLogout("all")}
                  className="text-red-600 dark:text-red-400"
                >
                  <LogOut className="mr-2 h-4 w-4" />
                  <span>{t("logOut")}</span>
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </>
  );

  return (
    <>
      {isMobile ? (
        <Dialog open={isOpen} onOpenChange={setIsOpen}>
          <DialogTrigger asChild>
            <button type="button" className={cn("fixed top-2 left-2 z-40 rounded-lg border p-2", colors.bg.card)}
              aria-label={t("toggleMenu")}><Menu className="h-5 w-5" /></button>
          </DialogTrigger>
          <DialogContent aria-describedby={undefined}
            className="inset-y-0 left-0 top-0 flex h-dvh w-64 max-w-full translate-x-0 translate-y-0 flex-col gap-0 rounded-none p-0 sm:rounded-none">
            <DialogTitle className="sr-only">{t("toggleMenu")}</DialogTitle>
            {sidebarContent}
          </DialogContent>
        </Dialog>
      ) : (
        <aside className={utilCn("relative hidden w-64 flex-col border-r md:flex", colors.bg.card, colors.border.default)}>
          {sidebarContent}
        </aside>
      )}

      {betaInvitesEnabled && (
        <BetaInviteDialog
          open={inviteDialogOpen}
          onOpenChange={setInviteDialogOpen}
          summary={betaInvites.summary}
          error={betaInvites.error}
          isLoading={betaInvites.isLoading}
          loadMore={betaInvites.loadMore}
          create={betaInvites.create}
          revoke={betaInvites.revoke}
          reissue={betaInvites.reissue}
        />
      )}
    </>
  );
}
