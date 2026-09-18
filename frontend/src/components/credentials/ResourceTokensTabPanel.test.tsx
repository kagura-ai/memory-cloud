/**
 * ResourceTokensTabPanel — XL-only create gate (#1551).
 *
 * "May create" vs "may serve": tokens that already exist on M / L stay listed
 * (and editable / revocable) while the Create button is disabled behind an
 * upsell naming the XL tier; on XL the button is live and the only remaining
 * prerequisite is a context with a resource_id.
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

// #1560: the create gate is the tier matrix's `resources` boolean via
// usePlanFeature (tri-state; `null` = resolving), not a tier-name rank.
let mockPlanFeature: boolean | null = true;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanFeature: () => mockPlanFeature,
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
