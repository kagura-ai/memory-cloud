/**
 * ResourceTokensTabPanel — XL-only create gate (#1551).
 *
 * "May create" vs "may serve": tokens that already exist on M / L stay listed
 * (and editable / revocable) while the Create button is disabled behind an
 * upsell naming the XL tier; on XL the button is live and the only remaining
 * prerequisite is a context with a resource_id.
 *
 * #1643: the upsell's link to the Plan page is withheld wherever that page is
 * not reachable; the upsell copy itself always renders.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ResourceTokensTabPanel } from "./ResourceTokensTabPanel";

const mockListResourceTokens = vi.fn();
const mockGetContexts = vi.fn();
vi.mock("@/lib/api/resource-tokens", () => ({
  listResourceTokens: (...args: unknown[]) => mockListResourceTokens(...args),
  revokeResourceToken: vi.fn(),
  updateResourceToken: vi.fn(),
}));
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...args: unknown[]) => mockGetContexts(...args),
}));

let mockCurrentWorkspace: {
  plan_name?: string;
  current_user_role?: string;
} | null = null;
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: "ws-1",
    currentWorkspace: mockCurrentWorkspace,
  }),
}));

// #1560: the create gate is the tier matrix's `resources` boolean, not a
// tier-name rank. #1645: read through useFeatureGate; the tri-state below maps
// onto its descriptor (`null` = resolving = "pending").
let mockPlanFeature: boolean | null = true;
const MOCK_GATES = {
  null: { state: "pending", feature: "resources", canUpgrade: false },
  true: { state: "allowed", feature: "resources", canUpgrade: false },
  false: {
    state: "plan",
    feature: "resources",
    requiredPlan: "promax",
    planLabel: "XL",
    canUpgrade: false,
  },
} as const;
vi.mock("@/hooks/useFeatureGate", () => ({
  useFeatureGate: () => MOCK_GATES[`${mockPlanFeature}`],
}));

// #1643: useCanUpgrade reads /system/info. Without this mock the real hook
// fires a jsdom fetch, retries three times and leaves a module-level cache
// that leaks between cases in this file. `null` = still resolving.
let mockFeatures: Record<string, boolean> | null = { plan_page: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

// #1560: the SERVE caps ("used / max", capacity) come from the owner-only
// GET /workspaces/{id}/plan, not a hand-mirrored per-tier table.
const mockGetWorkspacePlan = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getWorkspacePlan: (...args: unknown[]) => mockGetWorkspacePlan(...args),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// Child surfaces are not under test — render the token ids so "existing
// tokens are still listed" is observable, and keep the dialog inert.
vi.mock("@/components/resource-tokens/ResourceTokensTable", () => ({
  ResourceTokensTable: ({ tokens }: { tokens: { id: number }[] }) => (
    <ul>
      {tokens.map((tk) => (
        <li key={tk.id}>token#{tk.id}</li>
      ))}
    </ul>
  ),
}));
vi.mock("@/components/resource-tokens/CreateResourceTokenDialog", () => ({
  CreateResourceTokenDialog: () => null,
}));

const token = (id: number) => ({
  id,
  resource_id: "products",
  description: null,
  quota_events_per_hour: 1000,
  created_by: "owner-1",
  created_at: "2026-01-01T00:00:00Z",
  last_used_at: null,
  is_active: true,
  status: "active" as const,
});

const resourceContext = {
  id: "ctx-1",
  name: "products",
  resource_id: "products",
};

beforeEach(() => {
  mockListResourceTokens.mockReset();
  mockGetContexts.mockReset();
  mockGetWorkspacePlan.mockReset();
  mockGetWorkspacePlan.mockResolvedValue({
    quotas: { max_resource_tokens: 30, max_quota_capacity: 300000 },
  });
  mockCurrentWorkspace = { plan_name: "promax", current_user_role: "owner" };
  mockPlanFeature = true; // #1560: resources included unless a test says otherwise
  mockFeatures = { plan_page: true };
});

const createButton = () => screen.getByRole("button", { name: /createToken/ });

describe("ResourceTokensTabPanel — XL-only create gate (#1551)", () => {
  it.each([["basic"], ["pro"]])(
    "%s: existing tokens stay listed, Create is disabled behind the XL upsell",
    async (plan) => {
      mockCurrentWorkspace = { plan_name: plan, current_user_role: "owner" };
      mockPlanFeature = false; // #1560: the matrix says resources=false here
      mockListResourceTokens.mockResolvedValue({
        tokens: [token(1), token(2)],
        total: 2,
      });
      mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

      render(<ResourceTokensTabPanel />);

      expect(await screen.findByText("token#1")).toBeInTheDocument();
      expect(screen.getByText("token#2")).toBeInTheDocument();
      expect(createButton()).toBeDisabled();
      expect(screen.getByText("planGateTitle")).toBeInTheDocument();
      // The resource-id hint is the OTHER prerequisite — not shown here.
      expect(screen.queryByText("noResourceIdWarning")).toBeNull();
    },
  );

  it("promax with a resource-id context: Create enabled, no upsell", async () => {
    mockListResourceTokens.mockResolvedValue({ tokens: [token(1)], total: 1 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("token#1")).toBeInTheDocument();
    await waitFor(() => expect(createButton()).toBeEnabled());
    expect(screen.queryByText("planGateTitle")).toBeNull();
    expect(screen.queryByText("noResourceIdWarning")).toBeNull();
  });

  it("promax without a resource-id context: only the resource-id hint gates Create", async () => {
    mockListResourceTokens.mockResolvedValue({ tokens: [], total: 0 });
    mockGetContexts.mockResolvedValue({
      contexts: [{ id: "ctx-2", name: "plain", resource_id: null }],
    });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("noResourceIdWarning")).toBeInTheDocument();
    expect(createButton()).toBeDisabled();
    expect(screen.queryByText("planGateTitle")).toBeNull();
  });

  // #1560: the gate follows the API boolean, not the tier's name/rank.
  it("pro with resources=true from the matrix: Create enabled, no upsell (#1560)", async () => {
    mockCurrentWorkspace = { plan_name: "pro", current_user_role: "owner" };
    mockPlanFeature = true;
    mockListResourceTokens.mockResolvedValue({ tokens: [token(1)], total: 1 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("token#1")).toBeInTheDocument();
    await waitFor(() => expect(createButton()).toBeEnabled());
    expect(screen.queryByText("planGateTitle")).toBeNull();
  });

  it("pending gate: Create disabled with no upsell while the matrix resolves (#1560)", async () => {
    mockPlanFeature = null;
    mockListResourceTokens.mockResolvedValue({ tokens: [token(1)], total: 1 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("token#1")).toBeInTheDocument();
    expect(createButton()).toBeDisabled();
    expect(screen.queryByText("planGateTitle")).toBeNull();
    expect(screen.queryByText("noResourceIdWarning")).toBeNull();
  });
});

describe("ResourceTokensTabPanel — upgrade link gate (#1643)", () => {
  async function renderGated() {
    mockCurrentWorkspace = { plan_name: "pro", current_user_role: "owner" };
    mockPlanFeature = false;
    mockListResourceTokens.mockResolvedValue({ tokens: [], total: 0 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    // The plan-gate copy is the explanation — it renders in every case here.
    expect(await screen.findByText("planGateTitle")).toBeInTheDocument();
    expect(screen.getByText("planGateDesc")).toBeInTheDocument();
  }

  it("owner below XL, plan_page off: the plan-gate copy renders with no upgrade link", async () => {
    mockFeatures = {};
    await renderGated();

    expect(screen.queryByRole("link", { name: "upgradePlan" })).toBeNull();
  });

  it("owner below XL, /system/info pending: no upgrade link", async () => {
    mockFeatures = null;
    await renderGated();

    expect(screen.queryByRole("link", { name: "upgradePlan" })).toBeNull();
  });

  it("owner below XL, plan_page on: the plan-gate copy renders with an upgrade link", async () => {
    await renderGated();

    expect(screen.getByRole("link", { name: "upgradePlan" })).toHaveAttribute(
      "href",
      "/workspace/settings/plan",
    );
  });
});

describe("ResourceTokensTabPanel — serve caps from /workspaces/{id}/plan (#1560)", () => {
  it("owner: renders used / max and the capacity line from the plan quotas", async () => {
    mockListResourceTokens.mockResolvedValue({
      tokens: [token(1), token(2)],
      total: 2,
    });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("token#1")).toBeInTheDocument();
    expect(mockGetWorkspacePlan).toHaveBeenCalledWith("ws-1");
    // "2 / 30" — the 30 is the API's max_resource_tokens, not a local table.
    expect(await screen.findByText(/\/ 30/)).toBeInTheDocument();
    expect(screen.getByText("maxCapacity")).toBeInTheDocument();
  });

  it("owner: withholds the caps (no stale table) when the plan fetch fails", async () => {
    mockGetWorkspacePlan.mockRejectedValue(new Error("boom"));
    mockListResourceTokens.mockResolvedValue({ tokens: [token(1)], total: 1 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("token#1")).toBeInTheDocument();
    await waitFor(() => expect(mockGetWorkspacePlan).toHaveBeenCalled());
    expect(screen.queryByText(/\/ \d+/)).toBeNull();
    expect(screen.queryByText("maxCapacity")).toBeNull();
  });

  it("owner → non-owner switch: the previous workspace's caps are cleared", async () => {
    mockListResourceTokens.mockResolvedValue({ tokens: [token(1)], total: 1 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    const { rerender } = render(<ResourceTokensTabPanel />);
    expect(await screen.findByText(/\/ 30/)).toBeInTheDocument();
    expect(screen.getByText("maxCapacity")).toBeInTheDocument();

    // Same panel instance, viewer no longer owns the workspace. Non-owners
    // never fetch the plan, so the effect must drop the stale figures rather
    // than leave the previous workspace's owner-only caps on screen.
    mockCurrentWorkspace = { plan_name: "promax", current_user_role: "admin" };
    rerender(<ResourceTokensTabPanel />);

    await waitFor(() => expect(screen.queryByText(/\/ 30/)).toBeNull());
    expect(screen.queryByText("maxCapacity")).toBeNull();
    expect(mockGetWorkspacePlan).toHaveBeenCalledTimes(1);
  });

  it("non-owner: never calls the owner-only plan endpoint", async () => {
    mockCurrentWorkspace = { plan_name: "promax", current_user_role: "admin" };

    render(<ResourceTokensTabPanel />);

    // Non-owners skip both owner-only loads (tokens and plan); flush effects
    // and confirm neither fired and no cap figure is on screen.
    await Promise.resolve();
    expect(mockListResourceTokens).not.toHaveBeenCalled();
    expect(mockGetWorkspacePlan).not.toHaveBeenCalled();
    expect(screen.queryByText("maxCapacity")).toBeNull();
  });
});
