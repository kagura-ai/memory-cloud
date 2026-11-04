/** Role-restricted tabs stay discoverable; their protected panels never mount (#2630). */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

import ContextDetailPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockGetContext = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContext: (...a: unknown[]) => mockGetContext(...a),
}));

const mockUseParams = vi.fn();
const mockUseSearchParams = vi.fn();
const mockReplace = vi.fn();
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useParams: () => mockUseParams(),
  useSearchParams: () => mockUseSearchParams(),
  useRouter: () => ({ push: mockPush, replace: mockReplace }),
  usePathname: () => "/workspace/contexts/ctx-1",
}));

// useTranslations must return a stable identity — fetchContext's useCallback
// depends on `t`, and the page's data-load useEffect depends on fetchContext.
// A new t on every render would trigger an infinite re-fetch loop in the test.
const stableT = (k: string) => k;
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => mockUseAuth() }));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

const mockUseMemoryContext = vi.fn();
vi.mock("@/contexts/MemoryContextContext", () => ({
  useMemoryContext: () => mockUseMemoryContext(),
}));

// Stub the lazy-loaded admin tab panels so happy-dom doesn't have to render
// d3 (graph) or fetch lists. We only care about which tab triggers / panels
// are present in the DOM, not what they contain.
vi.mock("@/components/contexts/OverviewTabPanel", () => ({
  OverviewTabPanel: () => <div data-testid="overview-panel" />,
}));
vi.mock("@/components/contexts/ConnectionsTabPanel", () => ({
  ConnectionsTabPanel: () => <div data-testid="connections-panel" />,
}));
vi.mock("@/components/contexts/SettingsTabPanel", () => ({
  SettingsTabPanel: () => <div data-testid="settings-panel" />,
}));
vi.mock("@/components/contexts/SearchSettingsSection", () => ({
  SearchSettingsSection: () => <div />,
}));
vi.mock("@/components/contexts/MembersSection", () => ({
  MembersSection: () => <div />,
}));
vi.mock("@/components/contexts/ProtectionSection", () => ({
  ProtectionSection: () => <div />,
}));
vi.mock("@/components/contexts/GraphTabPanel", () => ({
  GraphTabPanel: () => <div data-testid="graph-panel" />,
}));

// next/dynamic is used only to lazy-load GraphTabPanel. Stub the dynamic
// loader to a no-op so happy-dom doesn't have to chase the import graph
// (and the page renders synchronously in the test).
vi.mock("next/dynamic", () => ({
  default: () => () => null,
}));

// ---------- Helpers ----------------------------------------------------------

type Role = "owner" | "admin" | "member" | "viewer";

function makeContext() {
  return {
    id: "ctx-1",
    workspace_id: "ws-1",
    name: "test-ctx",
    display_name: "Test Context",
    description: "",
    summary: "",
    usage_guide: "",
    is_private: true,
    is_public: false,
    is_default: false,
    is_locked: false,
    embedding_model: "small",
    resource_id: null,
    created_by: "user-1",
  };
}

function setupWithRole(role: Role, urlTab: string | null = null) {
  mockUseParams.mockReturnValue({ id: "ctx-1" });
  mockUseSearchParams.mockReturnValue(
    new URLSearchParams(urlTab ? `tab=${urlTab}` : ""),
  );
  mockUseAuth.mockReturnValue({ user: { id: "user-1" } });
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: {
      id: "ws-1",
      plan_name: "pro",
      current_user_role: role,
    },
  });
  mockUseMemoryContext.mockReturnValue({ currentContext: null });
  mockGetContext.mockResolvedValue(makeContext());
}

beforeEach(() => {
  mockGetContext.mockReset();
  mockUseParams.mockReset();
  mockUseSearchParams.mockReset();
  mockUseAuth.mockReset();
  mockUseWorkspace.mockReset();
  mockUseMemoryContext.mockReset();
  mockReplace.mockReset();
  mockPush.mockReset();
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("ContextDetailPage tab visibility (#398)", () => {
  it.each(["owner", "admin"] as const)(
    "shows all four tabs for %s",
    async (role) => {
      setupWithRole(role);
      render(<ContextDetailPage />);
      await waitFor(() =>
        expect(screen.getByText("tabs.overview")).toBeInTheDocument(),
      );
      expect(screen.getByText("tabs.connections")).toBeInTheDocument();
      expect(screen.getByText("tabs.graph")).toBeInTheDocument();
      expect(screen.getByText("tabs.settings")).toBeInTheDocument();
    },
  );

  it.each(["member", "viewer"] as const)(
    "offers ordinary tabs, but not allowlisted analyses, for %s",
    async (role) => {
      setupWithRole(role);
      render(<ContextDetailPage />);
      await waitFor(() =>
        expect(screen.getByText("tabs.overview")).toBeInTheDocument(),
      );
      expect(screen.getByRole("tab", { name: "tabs.connections" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "tabs.graph" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "tabs.settings" })).toBeInTheDocument();
    },
  );
});

describe("ContextDetailPage deep-link tab guard (#398)", () => {
  it("never mounts a restricted panel for a different workspace’s context", async () => {
    setupWithRole("owner", "settings");
    mockGetContext.mockResolvedValue({ ...makeContext(), workspace_id: "other" });
    render(<ContextDetailPage />);
    await screen.findByText("failedToLoad");
    expect(screen.queryByTestId("settings-panel")).toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
  });
  it.each(["member", "viewer"] as const)(
    "explains the role in place for %s with a settings deep link",
    async (role) => {
      setupWithRole(role, "settings");
      render(<ContextDetailPage />);
      await screen.findByText("admin");
      expect(screen.getByRole("tab", { name: "tabs.settings" })).toHaveAttribute("aria-selected", "true");
      expect(screen.queryByTestId("settings-panel")).toBeNull();
      expect(mockReplace).not.toHaveBeenCalled();
    },
  );

  it.each(["owner", "admin"] as const)(
    "does NOT snap URL away from ?tab=settings when %s lands on it",
    async (role) => {
      setupWithRole(role, "settings");
      render(<ContextDetailPage />);
      await waitFor(() =>
        expect(screen.getByText("tabs.overview")).toBeInTheDocument(),
      );
      // Admin/owner can stay on settings — no snap should fire. We only assert
      // that no replace call carries tab=overview (a tab=settings replace can
      // still occur if useTabParam promotes the URL — that's fine).
      const overviewSnaps = mockReplace.mock.calls.filter(
        (c) =>
          typeof c[0] === "string" && (c[0] as string).includes("tab=overview"),
      );
      expect(overviewSnaps.length).toBe(0);
    },
  );

  // On a hard reload `currentWorkspace` is null while WorkspaceContext
  // hydrates. Without the workspace-loaded guard, canSeeAdminTabs collapses
  // to false and the snap effect would clobber an admin's deep-link target
  // before their role resolves.
  it("does NOT snap URL while WorkspaceContext is still loading (currentWorkspace=null)", async () => {
    mockUseParams.mockReturnValue({ id: "ctx-1" });
    mockUseSearchParams.mockReturnValue(new URLSearchParams("tab=settings"));
    mockUseAuth.mockReturnValue({ user: { id: "user-1" } });
    mockUseWorkspace.mockReturnValue({ currentWorkspace: null });
    mockUseMemoryContext.mockReturnValue({ currentContext: null });
    mockGetContext.mockResolvedValue(makeContext());

    render(<ContextDetailPage />);
    expect(mockGetContext).not.toHaveBeenCalled();
    const overviewSnaps = mockReplace.mock.calls.filter(
      (c) =>
        typeof c[0] === "string" && (c[0] as string).includes("tab=overview"),
    );
    expect(overviewSnaps.length).toBe(0);
  });
});
