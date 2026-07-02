/**
 * Tests for the Workspace > Plan page (#1141).
 *
 * Covers the currency/amount drift fix:
 *   - the page NEVER renders a hardcoded `$` price (pricePerMonth removed) —
 *     price/currency is owned by the payment service, not memory-cloud.
 *   - subscribed (paid tier) owners get the "review or change" button + a hint
 *     pointing at the billing portal for the real amount.
 *   - free (unsubscribed) owners keep the original "change plan" wording and
 *     see no billing-amount hint.
 *   - non-owners see the owner-only note and no billing button.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import WorkspacePlanPage from "./page";

// ---------- Mocks ------------------------------------------------------------

// Translator stub: surfaces the i18n key (plus interpolated price, if any) so
// the test can assert on key choice without depending on catalog wording.
const stableTranslator = (key: string, values?: Record<string, unknown>) =>
  values && "price" in values ? `${key}|${values.price}` : key;
vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
}));
vi.mock("@/i18n", () => ({ useLocale: () => ({ locale: "en" }) }));

let mockWorkspace: { current_user_role?: string; plan_name?: string } | null =
  null;
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: "ws-1",
    currentWorkspace: mockWorkspace,
  }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockGetWorkspacePlan = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  getWorkspacePlan: (...args: unknown[]) => mockGetWorkspacePlan(...args),
}));
const mockMintBillingHandoff = vi.fn();
vi.mock("@/lib/api/billing", () => ({
  mintBillingHandoff: (...args: unknown[]) => mockMintBillingHandoff(...args),
}));
vi.mock("@/lib/api/base", () => ({
  ApiError: class ApiError extends Error {
    status = 0;
  },
}));
vi.mock("@/lib/utils/planLabel", () => ({
  planLabelFromEnv: (tier: string) => tier,
}));
// The comparison matrix self-fetches and is covered by its own test; stub it
// here so this suite stays focused on the page's plan/billing concerns (#1138).
vi.mock("@/components/plan/PlanFeatureMatrix", () => ({
  PlanFeatureMatrix: () => null,
}));
// #1145: the page is gated behind the backend ENABLE_PLAN_PAGE flag. Default
// the mocked hook to enabled; flip per-test for the disabled-notice case.
let mockFeatures: Record<string, boolean> | null = { plan_page: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

const planInfo = (overrides: Record<string, unknown> = {}) => ({
  workspace_id: "ws-1",
  workspace_name: "WS",
  current_plan: "basic",
  plan_display_name: "Starter",
  // Intentionally a legacy USD value — the page must NOT surface it.
  price_monthly: 10,
  usage: { memories: 0, contexts: 0 },
  quotas: {
    memory_limit: 1,
    max_contexts: 1,
    mcp_calls_per_day: 1,
    mcp_calls_per_week: 1,
    rest_calls_per_day: 1,
    public_calls_per_day: 1,
  },
  can_upgrade: false,
  can_downgrade: false,
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  mockFeatures = { plan_page: true };
  mockGetWorkspacePlan.mockResolvedValue(planInfo());
});

// ---------- Tests ------------------------------------------------------------

describe("WorkspacePlanPage (#1141)", () => {
  it("renders a not-available notice when the Plan feature is disabled (#1145)", async () => {
    mockFeatures = { plan_page: false };
    mockWorkspace = { current_user_role: "owner", plan_name: "basic" };
    render(<WorkspacePlanPage />);
    expect(
      await screen.findByText("planPage.featureDisabled"),
    ).toBeInTheDocument();
    expect(screen.queryByText("planPage.currentPlan")).toBeNull();
    // Disabled → the owner-only plan fetch must be skipped (no wasted call).
    expect(mockGetWorkspacePlan).not.toHaveBeenCalled();
  });

  it("never renders a hardcoded $ price", async () => {
    mockWorkspace = { current_user_role: "owner", plan_name: "basic" };
    render(<WorkspacePlanPage />);
    await screen.findByText("planPage.currentPlan");
    // The pricePerMonth key is gone, and no "$10" leaks through.
    expect(screen.queryByText(/planPage\.pricePerMonth/)).toBeNull();
    expect(document.body.textContent ?? "").not.toMatch(/\$\s*10/);
  });

  it("subscribed (paid) owner sees the review-or-change button + billing hint", async () => {
    mockWorkspace = { current_user_role: "owner", plan_name: "basic" };
    render(<WorkspacePlanPage />);
    expect(
      await screen.findByText("planPage.reviewOrChangePlan"),
    ).toBeInTheDocument();
    expect(screen.getByText("planPage.billingAmountHint")).toBeInTheDocument();
    expect(screen.queryByText("planPage.manageBilling")).toBeNull();
  });

  it("free (unsubscribed) owner keeps the change-plan wording and shows no hint", async () => {
    mockWorkspace = { current_user_role: "owner", plan_name: "free" };
    render(<WorkspacePlanPage />);
    expect(
      await screen.findByText("planPage.manageBilling"),
    ).toBeInTheDocument();
    expect(screen.queryByText("planPage.reviewOrChangePlan")).toBeNull();
    expect(screen.queryByText("planPage.billingAmountHint")).toBeNull();
  });

  it("keeps the button label stable while the handoff is in flight (no flicker)", async () => {
    mockWorkspace = { current_user_role: "owner", plan_name: "basic" };
    let resolveHandoff: (v: { url?: string }) => void = () => {};
    mockMintBillingHandoff.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveHandoff = resolve;
        }),
    );
    render(<WorkspacePlanPage />);
    const button = await screen.findByRole("button", {
      name: /planPage\.reviewOrChangePlan/,
    });
    fireEvent.click(button);
    // In flight the visible label must NOT swap (billing-disabled deployments
    // reject in milliseconds → a swap reads as a flicker). Busy state = spinner
    // + disabled + sr-only announcement instead.
    expect(screen.getByText("planPage.reviewOrChangePlan")).toBeInTheDocument();
    expect(screen.getByText("planPage.opening")).toHaveClass("sr-only");
    expect(button).toBeDisabled();
    resolveHandoff({});
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("non-owner sees the owner-only note and no billing button", async () => {
    mockWorkspace = { current_user_role: "member", plan_name: "basic" };
    render(<WorkspacePlanPage />);
    expect(await screen.findByText("planPage.ownerOnly")).toBeInTheDocument();
    expect(screen.queryByText("planPage.reviewOrChangePlan")).toBeNull();
    expect(screen.queryByText("planPage.manageBilling")).toBeNull();
  });
});
