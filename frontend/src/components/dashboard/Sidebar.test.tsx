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

import { Sidebar } from "./Sidebar";

describe("Sidebar", () => {
  beforeEach(() => {
    vi.clearAllMocks();
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
    // Issue #921: the version comes from GET /api/v1/system/info, fetched at most
    // once per session and only when the user menu opens (onOpenChange) — never
    // eagerly on mount.
    expect(mockSystemInfoGet).not.toHaveBeenCalled();
  });
});
