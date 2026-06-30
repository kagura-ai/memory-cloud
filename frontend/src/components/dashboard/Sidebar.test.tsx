import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// Hoisted so the vi.mock factory below can reference it (vi.mock is hoisted).
const mockSystemInfoGet = vi.hoisted(() =>
  vi.fn().mockResolvedValue({ version: "9.9.9" }),
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
  role: "user" as const,
};

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: mockUser,
    logout: vi.fn(),
    isAuthenticated: true,
  }),
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: {
      id: "w1",
      name: "Test WS",
      current_user_role: "owner",
      member_count: 1,
    },
    currentWorkspaceId: "w1",
  }),
}));

vi.mock("@/lib/api/contexts", () => ({
  getContexts: vi.fn().mockResolvedValue({ contexts: [] }),
}));

vi.mock("@/lib/api/external-keys", () => ({
  listExternalAPIKeys: vi.fn().mockResolvedValue([]),
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
let mockFeatures: Record<string, boolean> | null = { plan_page: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

import { Sidebar } from "./Sidebar";

describe("Sidebar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFeatures = { plan_page: true };
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

  it("shows the owner Plan link wired to the settings/plan route (#1121/#1126)", () => {
    render(<Sidebar />);
    // next-intl is mocked to echo the key, so the nav label is "plan".
    const planLink = screen.getByRole("link", { name: "plan" });
    expect(planLink).toHaveAttribute("href", "/workspace/settings/plan");
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
