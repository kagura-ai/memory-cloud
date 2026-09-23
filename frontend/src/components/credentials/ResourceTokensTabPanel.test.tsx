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
 *
 * #1646: the upsell is FeatureGateNotice (inline, scope "create") replacing
 * the light-only purple div — under the key-echo mock its text is the
 * relative gate key ("plan.newTitle"), and its CTA is a button that pushes
 * the Plan page. The resource-id prerequisite block is unchanged.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FeatureGate } from "@/lib/gates/featureGates";

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
const mockGateFor = (planFeature: boolean | null): FeatureGate =>
  planFeature === null
    ? { state: "pending", feature: "resources", canUpgrade: false }
    : planFeature
      ? { state: "allowed", feature: "resources", canUpgrade: false }
      : {
          state: "plan",
          feature: "resources",
          requiredPlan: "promax",
          planLabel: "XL",
          // #1646: the notice reads the descriptor's own canUpgrade. The real
          // hook derives it from /system/info (pinned in
          // useFeatureGate.test.tsx); this stands in for that derivation so
          // the #1643 cases below keep driving it through `mockFeatures`.
          canUpgrade: mockFeatures?.plan_page === true,
        };
// A test that needs a descriptor the tri-state cannot express sets this.
let mockGate: FeatureGate | null = null;
vi.mock("@/hooks/useFeatureGate", () => ({
  useFeatureGate: () => mockGate ?? mockGateFor(mockPlanFeature),
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
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
  // #1646: FeatureGateNotice's CTA pushes the Plan page.
  useRouter: () => ({ push: mockPush }),
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
  mockGate = null;
  mockPush.mockReset();
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
      expect(screen.getByText("plan.newTitle")).toBeInTheDocument();
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
    expect(screen.queryByText("plan.newTitle")).toBeNull();
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
    expect(screen.queryByText("plan.newTitle")).toBeNull();
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
    expect(screen.queryByText("plan.newTitle")).toBeNull();
  });

  it("pending gate: Create disabled with no upsell while the matrix resolves (#1560)", async () => {
    mockPlanFeature = null;
    mockListResourceTokens.mockResolvedValue({ tokens: [token(1)], total: 1 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("token#1")).toBeInTheDocument();
    expect(createButton()).toBeDisabled();
    expect(screen.queryByText("plan.newTitle")).toBeNull();
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
    expect(await screen.findByText("plan.newTitle")).toBeInTheDocument();
    expect(screen.getByText("plan.newDescription")).toBeInTheDocument();
  }

  it("owner below XL, plan_page off: the plan-gate copy renders with no upgrade CTA", async () => {
    mockFeatures = {};
    await renderGated();

    expect(screen.queryByRole("button", { name: "plan.action" })).toBeNull();
    expect(screen.queryByRole("link", { name: "upgradePlan" })).toBeNull();
  });

  it("owner below XL, /system/info pending: no upgrade CTA", async () => {
    mockFeatures = null;
    await renderGated();

    expect(screen.queryByRole("button", { name: "plan.action" })).toBeNull();
  });

  it("owner below XL, plan_page on: the CTA pushes the Plan page", async () => {
    await renderGated();

    fireEvent.click(screen.getByRole("button", { name: "plan.action" }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
    // The old bare <a> is gone.
    expect(screen.queryByRole("link", { name: "upgradePlan" })).toBeNull();
  });
});

describe("ResourceTokensTabPanel — FeatureGateNotice (#1646 P5)", () => {
  it("the plan notice is the dark-safe Alert, not the light-only purple div", async () => {
    mockPlanFeature = false;
    mockListResourceTokens.mockResolvedValue({ tokens: [], total: 0 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    const title = await screen.findByText("plan.newTitle");
    const notice = title.closest('[role="alert"]');
    expect(notice).not.toBeNull();
    // The Alert `upsell` variant carries its own dark tokens; the old div
    // (border-2 border-purple-200, no dark: classes) is gone.
    expect(notice!.className).toMatch(/dark:bg-purple-950/);
    expect(notice!.className).not.toMatch(/border-2/);
    // scope "create": the create-scoped copy, not the whole-feature copy.
    expect(screen.queryByText("plan.title")).toBeNull();
    expect(screen.queryByText("planGateTitle")).toBeNull();
  });

  it("no served tier has resources: the tier-less copy, no CTA", async () => {
    mockGate = { state: "plan", feature: "resources", canUpgrade: true };
    mockListResourceTokens.mockResolvedValue({ tokens: [token(1)], total: 1 });
    mockGetContexts.mockResolvedValue({ contexts: [resourceContext] });

    render(<ResourceTokensTabPanel />);

    expect(await screen.findByText("token#1")).toBeInTheDocument();
    expect(screen.getByText("plan.titleNoTier")).toBeInTheDocument();
    expect(screen.getByText("plan.descriptionNoTier")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "plan.action" })).toBeNull();
    expect(createButton()).toBeDisabled();
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
