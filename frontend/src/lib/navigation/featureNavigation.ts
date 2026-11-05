import type { ElementType } from "react";
import { Role, WorkspaceRole } from "@/lib/auth/rbac";
import { Compass, Activity, Brain, Key, Settings, Puzzle, Database, Sliders, KeyRound, Users, BarChart, CreditCard, Lock, Gauge, Moon, ShieldCheck, DollarSign, Plug, HardDrive } from "lucide-react";

export interface NavItem {
  nameKey: string; // Translation key
  href: string;
  exact?: boolean;
  activePaths?: string[];
  icon: ElementType;
  requiredRole?: Role; // System admin role
  requiredWorkspaceRole?: Exclude<WorkspaceRole, WorkspaceRole.Viewer>; // Workspace role (minimum required)
  requiredFeature?: string | string[]; // Issue #1145: gate on GET /system/info features flag(s); every listed flag must be true (#1571)
  existingObjectsFallback?: 'externalKeys'; // Issue #1616: a requiredFeature miss still opens the page for a role that passes requiredWorkspaceRole
  planHint?: 'resources' | 'connectors' | 'team_invitations' | 'sleep_reports';
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
export const navigationGroups: NavGroup[] = [
  {
    titleKey: "workspace",
    collapsible: false,
    items: [
      {
        nameKey: "dashboard",
        href: "/workspace/dashboard",
        icon: BarChart,
        requiredWorkspaceRole: WorkspaceRole.Member,
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
        planHint: "resources",
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
        // Secret values and their metadata remain off limits below admin.
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
        planHint: "team_invitations",
        requiredWorkspaceRole: WorkspaceRole.Admin,
      },
      {
        // Sleep reports remain restricted to workspace administrators.
        nameKey: "sleepReports",
        href: "/workspace/sleep-reports",
        icon: Moon,
        planHint: "sleep_reports",
        requiredWorkspaceRole: WorkspaceRole.Admin,
      },
      {
        // Issue #473: workspace-scoped cost dashboard.
        // Issue #1167: part of the BYOK surface — hidden when ENABLE_BYOK
        // is off (the backing API 404s). Issue #1571: also hidden when the
        // deployment hides money (ENABLE_COST_DISPLAY=false) — both flags
        // must be on. Reporting entries stay last in the group:
        // sleepReports, then cost.
        nameKey: "cost",
        href: "/workspace/cost",
        icon: DollarSign,
        requiredWorkspaceRole: WorkspaceRole.Admin,
        requiredFeature: ["byok", "cost_display"],
      },
    ],
  },
  {
    titleKey: "integrations",
    collapsible: false,
    items: [
      { nameKey: "connectionGuide", href: "/workspace/integrations", icon: Compass, exact: true },
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
        planHint: "connectors",
      },
    ],
  },
  {
    titleKey: "settings",
    collapsible: false,
    items: [
      {
        // Billing data stays owner-only; the feature itself is discoverable.
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
        // The entry remains visible. With ENABLE_BYOK off only provisioning
        // (create / update) 404s; list, toggle and delete stay open to the
        // owner (#1613), whose console hides provisioning, so stored keys,
        // which the services still use, can be disabled or deleted (#1616).
        requiredFeature: "byok",
        existingObjectsFallback: "externalKeys",
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
        nameKey: "workerApps",
        href: "/admin/worker-apps",
        icon: Puzzle,
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

// The route catalog above keeps per-feature access metadata. The sidebar has
// one entry for each section; its pages provide the subordinate navigation.
export const sidebarNavigationGroups: NavGroup[] = navigationGroups.flatMap(group => {
  if (group.titleKey === "settings" || group.titleKey === "integrations") return [];
  if (group.titleKey === "workspace") return [{
    ...group,
    titleKey: "",
    items: [
      ...group.items.filter(item => item.nameKey !== "resources" && item.nameKey !== "secrets"),
      { nameKey: "integrations", href: "/workspace/integrations", icon: Plug, activePaths: ["/workspace/resources", "/workspace/secrets"] },
      { nameKey: "settings", href: "/workspace/settings", icon: Settings },
    ],
  }];
  return [group];
});

// Match feature routes without using object IDs or data from protected APIs.
const items = navigationGroups.flatMap((group) => group.items.flatMap((item) => [item, ...(item.children ?? [])]));
export function navigationForPath(pathname: string): NavItem | undefined {
  return items.find(({ href, exact }) => {
    const path = href.split("?")[0];
    return pathname === path || (!exact && pathname.startsWith(path + "/"));
  });
}
