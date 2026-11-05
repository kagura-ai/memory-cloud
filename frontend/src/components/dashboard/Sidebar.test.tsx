import { render, screen, within, act, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Hoisted so the vi.mock factory below can reference it (vi.mock is hoisted).
const mockSystemInfoGet = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ version: "9.9.9" }),
);
// #1495: the nav warning now asks the server whether embedding is AVAILABLE
// instead of counting the workspace's own key rows — a workspace served by the
// platform credential owns none and is perfectly healthy.
const mockKeyStatus = vi.hoisted(() =>
  vi.fn().mockResolvedValue({
    has_key: false,
    embedding_available: true,
    can_configure: true,
    external_keys_url: "/integrations/external-keys",
  }),
);

// Mock next-intl to return the translation key (matches KpiCards.test.tsx pattern)
vi.mock("next-intl", () => ({
  useTranslations:
    (_ns: string) => (key: string, vars?: Record<string, unknown>) => {
      if (vars && Object.keys(vars).length > 0) {
        return `${key}:${JSON.stringify(vars)}`;
      }
      return key;
    },
}));

// Mock next/navigation
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/workspace/dashboard",
  useSearchParams: () => new URLSearchParams(),
}));

// Mock contexts
const mockUser = {
  id: "u1",
  name: "Test User",
  email: "test@example.com",
  picture: "",
  role: "user" as "user" | "admin",
};

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: mockUser,
    logout: vi.fn(),
    isAuthenticated: true,
  }),
}));

// #1571: the existing-objects probe is module-cached per workspace id, so the
// plan-gate tests below give each case its own id instead of sharing "w1".
let mockWorkspaceId = "w1";
// #1616: the External Keys fallback probe is owner-only; flip per test.
let mockWorkspaceRole = "owner";
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: {
      id: mockWorkspaceId,
      name: "Test WS",
      current_user_role: mockWorkspaceRole,
      member_count: 1,
    },
    currentWorkspaceId: mockWorkspaceId,
  }),
}));

// #1571: resources / connectors are plan-gated with an existing-objects
// fallback probed through the list APIs. Default: every plan feature included
// (today's UI for an XL workspace); flip per test.
let mockPlanFeatures: Record<string, boolean> | null = {
  resources: true,
  connectors: true,
  public_contexts: true,
};
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanFeatures: () => mockPlanFeatures,
}));
const mockListResources = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ resources: [], total: 0 }),
);
const mockListConnectors = vi.hoisted(() => vi.fn().mockResolvedValue([]));
vi.mock("@/lib/api/resources", () => ({
  listResources: (...args: unknown[]) => mockListResources(...args),
}));
vi.mock("@/lib/api/workspace-connectors", () => ({
  listConnectors: (...args: unknown[]) => mockListConnectors(...args),
}));

vi.mock("@/lib/api/contexts", () => ({
  getContexts: vi.fn().mockResolvedValue({ contexts: [] }),
}));

// #1616: with BYOK off the External Keys entry falls back to "does the
// workspace already store a key?", probed through the owner-only list route.
const mockListExternalKeys = vi.hoisted(() => vi.fn().mockResolvedValue([]));
vi.mock("@/lib/api/external-keys", () => ({
  listExternalAPIKeys: (...args: unknown[]) => mockListExternalKeys(...args),
}));

vi.mock("@/lib/api/workspaces", () => ({
  checkOpenAIKeyStatus: (...args: unknown[]) => mockKeyStatus(...args),
}));

vi.mock("@/lib/api/base", () => ({
  apiClient: { get: mockSystemInfoGet },
}));

vi.mock("@/components/workspaces/WorkspaceSwitcher", () => ({
  WorkspaceSwitcher: () => <div data-testid="workspace-switcher" />,
}));

vi.mock("@/components/icons/KaguraLogo", () => ({
  KaguraLogo: ({ className }: { className?: string }) => (
    <svg data-testid="kagura-logo" className={className} />
  ),
}));

// #1145: feature flags gate certain nav items (e.g. Plan). Mock the hook so the
// suite doesn't hit the network; default to plan_page enabled, flip per-test.
// #1167: byok gates the externalKeys + workspace cost entries; default on.
// #1571: cost_display gates the workspace cost entry too; default on.
let mockFeatures: Record<string, boolean> | null = {
  plan_page: true,
  byok: true,
  cost_display: true,
};
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

import { Sidebar } from "./Sidebar";

describe("Sidebar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFeatures = { plan_page: true, byok: true, cost_display: true };
    mockPlanFeatures = {
      resources: true,
      connectors: true,
      public_contexts: true,
    };
    mockWorkspaceId = "w1";
    mockWorkspaceRole = "owner";
    mockUser.role = "user";
  });

  it("renders the Kagura logo at the top, linked to dashboard", () => {
    render(<Sidebar />);
    const logo = screen.getByTestId("kagura-logo");
    expect(logo).toBeInTheDocument();
    // The logo is wrapped in a Link to /workspace/dashboard
    const link = logo.closest("a");
    expect(link).toHaveAttribute("href", "/workspace/dashboard");
    expect(link).toHaveAttribute("aria-label", "kaguraLogoAria");
  });

  it("shows a single Settings destination", () => {
    render(<Sidebar />);
    // next-intl is mocked to echo the key, so the nav label is "plan".
    const planLink = screen.getByRole("link", { name: "settings" });
    expect(planLink).toHaveAttribute("href", "/workspace/settings");
  });

  it("hides the Plan link when the plan_page feature flag is off (#1145)", () => {
    mockFeatures = { plan_page: false };
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "plan" })).toBeNull();
  });

  it("hides the Plan link while feature flags are still loading (default-off) (#1145)", () => {
    mockFeatures = null;
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "plan" })).toBeNull();
  });

  it("shows the Integrations entry and deployment-enabled workspace cost", () => {
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "externalKeys" })).toBeNull();
    expect(screen.getByRole("link", { name: "integrations" })).toHaveAttribute("href", "/workspace/integrations");
    expect(document.querySelector('nav a[href="/workspace/cost"]')).toHaveAttribute(
      "href",
      "/workspace/cost",
    );
  });

  it("hides workspace cost when BYOK is off and keeps Sleep reports", () => {
    mockFeatures = { plan_page: true, byok: false };
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "externalKeys" })).toBeNull();
    expect(document.querySelector('nav a[href="/workspace/cost"]')).toBeNull();
    expect(
      document.querySelector('nav a[href="/workspace/sleep-reports"]'),
    ).toBeInTheDocument();
  });

  it("keeps deployment-gated workspace cost hidden while flags load", () => {
    mockFeatures = null;
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: "externalKeys" })).toBeNull();
    expect(document.querySelector('nav a[href="/workspace/cost"]')).toBeNull();
  });

  it("hides workspace cost when cost display is off and keeps Integrations", () => {
    mockFeatures = { plan_page: true, byok: true, cost_display: false };
    render(<Sidebar />);
    expect(document.querySelector('nav a[href="/workspace/cost"]')).toBeNull();
    expect(screen.queryByRole("link", { name: "externalKeys" })).toBeNull();
    expect(screen.getByRole("link", { name: "integrations" })).toHaveAttribute("href", "/workspace/integrations");
  });

  it("keeps the admin /admin/cost link for a system admin when cost_display is off (#1571)", () => {
    // Operators still need to see what the platform spends; only the
    // workspace-facing entry is money the deployment hides.
    mockUser.role = "admin";
    mockFeatures = { plan_page: true, byok: true, cost_display: false };
    render(<Sidebar />);
    const costLinks = screen.getAllByRole("link", { name: "cost" });
    expect(costLinks.map((a) => a.getAttribute("href"))).toEqual([
      "/admin/cost",
    ]);
  });

  it("skips the owner embedding-availability probe when byok is off (#1167)", () => {
    // The key-status route is behind `require_byok_enabled` and 404s in that
    // configuration (see byok-probe-guard.test.ts for the source-level guard).
    mockFeatures = { plan_page: true, byok: false };
    render(<Sidebar />);
    expect(mockKeyStatus).not.toHaveBeenCalled();
  });

  it("still probes embedding availability for owners when byok is on (#1167)", () => {
    // #1495 changed WHICH probe: the nav used to count the workspace's own key
    // rows, which warned every workspace served by the platform credential
    // that it was broken. The #1167 rule this pins — owners probe, others do
    // not — is unchanged.
    render(<Sidebar />);
    expect(mockKeyStatus).toHaveBeenCalledTimes(1);
  });

  it("orders the workspace group: stores first, reporting last (#1167)", () => {
    render(<Sidebar />);
    const expected = [
      "/workspace/dashboard",
      "/workspace/contexts",
      "/workspace/storage",
      "/workspace/members",
      "/workspace/sleep-reports",
      "/workspace/cost",
    ];
    // Scope to the <nav> so the logo link (also /workspace/dashboard) is
    // not counted twice.
    const hrefs = within(screen.getByRole("navigation"))
      .getAllByRole("link")
      .map((a) => a.getAttribute("href"))
      .filter((h): h is string => h !== null && expected.includes(h));
    expect(hrefs).toEqual(expected);
  });

  it("user menu trigger button shows user name", () => {
    render(<Sidebar />);
    expect(screen.getByText("Test User")).toBeInTheDocument();
  });

  it("renders the user menu trigger without name/email duplication", () => {
    render(<Sidebar />);
    // Trigger shows the name once
    const triggers = screen.getAllByText("Test User");
    expect(triggers).toHaveLength(1);
    // Email is NOT rendered anywhere by default (the old DropdownMenuLabel block was removed)
    expect(screen.queryByText("test@example.com")).not.toBeInTheDocument();
  });

  it("does not fetch the system version on initial render (lazy until the user menu opens)", () => {
    render(<Sidebar />);
    // Issue #921: the *version* display (via apiClient.get inside
    // handleUserMenuOpenChange) stays lazy — fetched at most once per session,
    // only when the user menu opens, never eagerly on mount.
    // NB: feature-flag loading (useSystemFeatures → /system/info) IS eager on
    // mount post-#1145; it is mocked away here and covered by
    // useSystemFeatures.test.tsx, so this assertion only guards the version path.
    expect(mockSystemInfoGet).not.toHaveBeenCalled();
  });
});

describe("mobile navigation", () => {
  it("keeps the closed drawer out of the tab order and returns focus on Escape", async () => {
    const original = window.matchMedia;
    window.matchMedia = (q: string) => ({ ...original.call(window, q), matches: true,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
    });
    try {
      render(<Sidebar />);
      expect(screen.queryByRole("navigation")).toBeNull();
      const trigger = screen.getByRole("button", { name: "toggleMenu" });
      fireEvent.click(trigger);
      const drawer = await screen.findByRole("dialog");
      expect(within(drawer).getByRole("navigation")).toBeInTheDocument();
      fireEvent.keyDown(drawer, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(trigger).toHaveFocus());
    } finally {
      window.matchMedia = original;
    }
  });
});

describe("feature discoverability (#2630)", () => {
  it.each(["owner", "admin", "member", "viewer"])(
    "keeps feature destinations available to %s without probing restricted lists",
    (role) => {
      vi.clearAllMocks();
      mockWorkspaceId = "discoverability-" + role;
      mockWorkspaceRole = role;
      mockUser.role = "user";
      mockFeatures = { plan_page: true, byok: true, cost_display: true };
      mockPlanFeatures = { resources: false, connectors: false, public_contexts: false };
      render(<Sidebar />);
      for (const href of ["/workspace/integrations",
        "/workspace/dashboard", "/workspace/settings",
        "/workspace/members"]) {
        expect(document.querySelector(`nav a[href="${href}"]`)).not.toBeNull();
      }
      expect(document.querySelector('nav a[href^="/admin/"]')).toBeNull();
      expect(document.querySelector('nav a[href="/workspace/resources"]')).toBeNull();
      expect(document.querySelector('nav a[href="/workspace/secrets"]')).toBeNull();
      expect(mockListResources).not.toHaveBeenCalled();
      expect(mockListConnectors).not.toHaveBeenCalled();
      expect(mockListExternalKeys).not.toHaveBeenCalled();
    },
  );
});

// BYOK visibility and retained-key management now live in WorkspaceSectionNavigation.test.tsx.

describe("the sidebar subtree survives a state change (#1488 Phase 4)", () => {
  /**
   * Regression guard for a defect that made the Phase 3 account switcher
   * unreachable in production.
   *
   * `SidebarContent` was a component defined INSIDE `Sidebar`. Every render
   * produced a new function, and a new function is a new component type to
   * React, so the whole subtree unmounted and remounted on any state change.
   * That discards state React does not own — including the Radix
   * `DropdownMenu`'s uncontrolled open flag, so the user menu closed itself.
   *
   * Opening that menu cannot be driven under happy-dom in this repo, so this
   * asserts the ROOT CAUSE instead of the symptom: after a state change, the
   * DOM nodes must be the same objects. A remount replaces them.
   *
   * The trigger here is the cross-tab `storage` listener (`setCollapsedSections`)
   * because it is deterministic and needs no open menu. Any `Sidebar` state
   * change would do — `setAccounts` from `refreshAccounts()` is the one that
   * actually fired on every menu open.
   */
  it("keeps the same DOM nodes when Sidebar state updates", () => {
    render(<Sidebar />);
    const before = screen.getByTestId("kagura-logo");
    const navBefore = before.closest("aside");

    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "sidebar-collapsed-sections",
          newValue: JSON.stringify({ admin: true }),
        }),
      );
    });

    const after = screen.getByTestId("kagura-logo");
    expect(after).toBe(before);
    expect(after.closest("aside")).toBe(navBefore);
  });
});
