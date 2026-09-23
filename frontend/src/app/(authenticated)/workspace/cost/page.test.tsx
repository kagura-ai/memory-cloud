/**
 * Workspace cost page — ENABLE_BYOK gate (#1167).
 *
 * The page is gated behind the backend byok feature flag (plan-page pattern
 * #1145): spinner while flags load, "not available" notice when off, and the
 * dashboard only renders (and fetches) when the flag resolves enabled.
 *
 * #1646 D1/D2: both flags are the one `cost_dashboard` gate, rendered by
 * FeatureGateNotice's page variant (`gate.deployment.*` — the key-echo
 * translator shows the relative key). A deployment notice has no CTA.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: (_ns?: string) => (k: string) => k,
  useLocale: () => "en",
}));

// FeatureGateNotice routes its CTA with the app router.
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

// useFeatureGate subscribes to the shared tier matrix; cost_dashboard has no
// matrix test, so it never waits on it. Mocked so jsdom never fetches.
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => null,
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
    expect(screen.getByText("deployment.title")).toBeInTheDocument();
    expect(screen.queryByTestId("cost-dashboard")).toBeNull();
  });

  it("renders the cost-display notice (not the dashboard) when cost_display is off (#1571)", () => {
    mockFeatures = { byok: true, cost_display: false };
    render(<WorkspaceCostPage />);
    expect(screen.getByText("deployment.title")).toBeInTheDocument();
    expect(screen.queryByTestId("cost-dashboard")).toBeNull();
    expect(mockFetchCost).not.toHaveBeenCalled();
  });

  it("renders a loader (not the dashboard) while feature flags load", () => {
    mockFeatures = null;
    render(<WorkspaceCostPage />);
    expect(screen.queryByTestId("cost-dashboard")).toBeNull();
    expect(screen.queryByText("deployment.title")).toBeNull();
    expect(screen.getByText("loading")).toBeInTheDocument();
  });
});

describe("WorkspaceCostPage deployment notice (#1646 D1/D2)", () => {
  function noticeText(features: Record<string, boolean>) {
    mockFeatures = features;
    const { container, unmount } = render(<WorkspaceCostPage />);
    const text = container.textContent;
    unmount();
    return text;
  }

  it("is the whole page: the cost title heads the deployment notice", () => {
    mockFeatures = { byok: false, cost_display: true };
    render(<WorkspaceCostPage />);
    expect(screen.getByRole("heading", { name: "title" })).toBeInTheDocument();
    expect(screen.getByText("deployment.description")).toBeInTheDocument();
  });

  it("byok off, cost_display off, both off and a failed /system/info render one and the same notice (C-19)", () => {
    const byokOff = noticeText({ byok: false, cost_display: true });
    expect(byokOff).toContain("deployment.title");
    expect(noticeText({ byok: true, cost_display: false })).toBe(byokOff);
    expect(noticeText({ byok: false, cost_display: false })).toBe(byokOff);
    // A failed /system/info resolves `{}`: every flag reads off (fail closed).
    expect(noticeText({})).toBe(byokOff);
  });

  it.each(["owner", "admin", "member"])(
    "never offers a CTA, even on a Plan-page deployment (role %s)",
    (role) => {
      setWorkspace(role);
      mockFeatures = { byok: false, cost_display: true, plan_page: true };
      render(<WorkspaceCostPage />);
      expect(screen.getByText("deployment.title")).toBeInTheDocument();
      expect(screen.queryAllByRole("button")).toHaveLength(0);
      expect(mockPush).not.toHaveBeenCalled();
    },
  );

  it("the deployment notice outranks the role check: a member sees it, not the role banner", () => {
    setWorkspace("member");
    mockFeatures = { byok: false, cost_display: true };
    render(<WorkspaceCostPage />);
    expect(screen.getByText("deployment.title")).toBeInTheDocument();
    expect(screen.queryByText("errors.forbiddenWorkspace")).toBeNull();
  });
});
