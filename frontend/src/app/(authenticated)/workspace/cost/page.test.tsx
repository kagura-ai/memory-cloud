/**
 * Workspace cost page — ENABLE_BYOK gate (#1167).
 *
 * The page is gated behind the backend byok feature flag (plan-page pattern
 * #1145): spinner while flags load, "not available" notice when off, and the
 * dashboard only renders (and fetches) when the flag resolves enabled.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: (_ns?: string) => (k: string) => k,
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

// #1571: cost_display gates the page too (fail-closed like byok); default on.
let mockFeatures: Record<string, boolean> | null = {
  byok: true,
  cost_display: true,
};
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

const mockFetchCost = vi.fn();
vi.mock("@/lib/api", () => ({
  fetchWorkspaceCostAggregation: (...a: unknown[]) => mockFetchCost(...a),
}));

vi.mock("@/components/cost/CostDashboard", () => ({
  CostDashboard: () => <div data-testid="cost-dashboard" />,
}));

import WorkspaceCostPage from "./page";

function setWorkspace(role: string = "admin") {
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: { id: "ws-1", current_user_role: role },
    currentWorkspaceId: "ws-1",
    loading: false,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockFeatures = { byok: true, cost_display: true };
  setWorkspace("admin");
});

afterEach(() => cleanup());

describe("WorkspaceCostPage BYOK gate (#1167)", () => {
  it("renders the dashboard when byok is enabled", () => {
    render(<WorkspaceCostPage />);
    expect(screen.getByTestId("cost-dashboard")).toBeInTheDocument();
  });

  it("renders the not-available notice when byok is off", () => {
    mockFeatures = { byok: false, cost_display: true };
    render(<WorkspaceCostPage />);
    expect(screen.getByText("featureDisabled")).toBeInTheDocument();
    expect(screen.queryByTestId("cost-dashboard")).toBeNull();
  });

  it("renders the cost-display notice (not the dashboard) when cost_display is off (#1571)", () => {
    mockFeatures = { byok: true, cost_display: false };
    render(<WorkspaceCostPage />);
    expect(screen.getByText("costDisplayDisabled")).toBeInTheDocument();
    expect(screen.queryByTestId("cost-dashboard")).toBeNull();
    expect(mockFetchCost).not.toHaveBeenCalled();
  });

  it("renders a loader (not the dashboard) while feature flags load", () => {
    mockFeatures = null;
    render(<WorkspaceCostPage />);
    expect(screen.queryByTestId("cost-dashboard")).toBeNull();
    expect(screen.queryByText("featureDisabled")).toBeNull();
  });
});
