/**
 * Tests for the workspace Sleep Reports plan gate (#1137 / #1548).
 *
 * Sleep Maintenance is Pro-or-better: free/basic see the upgrade CTA routing
 * to the Plan page; pro AND promax render the report list. The gate must not
 * fire while WorkspaceContext is still loading.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import WorkspaceSleepReportsPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

const stableT = (k: string) => k;
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
}));

let mockWorkspaceState: {
  currentWorkspace: { plan_name?: string; current_user_role?: string } | null;
  currentWorkspaceId: string | null;
  loading: boolean;
} = { currentWorkspace: null, currentWorkspaceId: null, loading: true };
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockWorkspaceState,
}));

vi.mock("@/lib/api", () => ({
  fetchWorkspaceSleepReports: vi.fn(),
}));

// The list self-fetches and is covered by its own tests; stub it to a marker
// so this suite stays focused on the gate.
vi.mock("@/components/sleep-reports/SleepReportsList", () => ({
  SleepReportsList: () => <div data-testid="sleep-reports-list" />,
}));

// ---------- Helpers ----------------------------------------------------------

function setWorkspace(plan_name: string, current_user_role = "owner") {
  mockWorkspaceState = {
    currentWorkspace: { plan_name, current_user_role },
    currentWorkspaceId: "ws-1",
    loading: false,
  };
}

beforeEach(() => {
  mockPush.mockReset();
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("WorkspaceSleepReportsPage plan gate", () => {
  it.each(["pro", "promax"] as const)(
    "%s renders the report list with no upgrade CTA",
    (plan) => {
      setWorkspace(plan);
      render(<WorkspaceSleepReportsPage />);
      expect(screen.getByTestId("sleep-reports-list")).toBeInTheDocument();
      expect(screen.queryByText("sleepReports.planGate.title")).toBeNull();
    },
  );

  it.each(["free", "basic"] as const)(
    "%s shows the upgrade CTA routing to the plan page",
    (plan) => {
      setWorkspace(plan);
      render(<WorkspaceSleepReportsPage />);
      expect(
        screen.getByText("sleepReports.planGate.title"),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("sleep-reports-list")).toBeNull();
      fireEvent.click(
        screen.getByRole("button", { name: "sleepReports.planGate.action" }),
      );
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
    },
  );

  it("does not gate while WorkspaceContext is still loading", () => {
    mockWorkspaceState = {
      currentWorkspace: null,
      currentWorkspaceId: null,
      loading: true,
    };
    render(<WorkspaceSleepReportsPage />);
    expect(screen.queryByText("sleepReports.planGate.title")).toBeNull();
    expect(mockPush).not.toHaveBeenCalled();
  });
});
