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
 *
 * #1646 D3: the ENABLE_PLAN_PAGE-off notice is the `plan_page` deployment
 * gate rendered by FeatureGateNotice (`gate.deployment.*` — the key-echo
 * translator shows the relative key), and it never carries a CTA.
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
  useLocale: () => "en",
}));
// FeatureGateNotice routes any CTA with the app router — there must be none.
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));
// useFeatureGate subscribes to the shared tier matrix; plan_page has no
// matrix test, so it never waits on it. Mocked so jsdom never fetches.
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => null,
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
// Keep the real tier predicates (isPlanTier / isPaidTier); echo the tier as
// its label so assertions don't depend on S/M/L/XL vs env overrides.
vi.mock("@/lib/utils/planLabel", async () => ({
  ...(await vi.importActual<typeof import("@/lib/utils/planLabel")>(
    "@/lib/utils/planLabel",
  )),
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
    max_resource_tokens: 3,
    max_quota_capacity: 30000,
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
    expect(await screen.findByText("deployment.title")).toBeInTheDocument();
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

  it("promax (paid) owner is subscribed: review-or-change button + billing hint (#1548)", async () => {
    mockWorkspace = { current_user_role: "owner", plan_name: "promax" };
    render(<WorkspacePlanPage />);
    expect(
      await screen.findByText("planPage.reviewOrChangePlan"),
    ).toBeInTheDocument();
    expect(screen.getByText("planPage.billingAmountHint")).toBeInTheDocument();
    expect(screen.queryByText("planPage.manageBilling")).toBeNull();
    // promax is a known tier → label resolves via planLabelFromEnv (echo),
    // not the backend display_name.
    expect(screen.getByText("promax")).toBeInTheDocument();
    expect(screen.queryByText("Starter")).toBeNull();
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
    // A persistent role="status" live region exists and is empty at rest, so
    // assistive tech reliably announces when its text flips (a conditionally
    // mounted sr-only span is not re-read on toggle).
    const liveRegion = screen.getByRole("status");
    expect(liveRegion).toHaveClass("sr-only");
    expect(liveRegion).toHaveTextContent("");

    fireEvent.click(button);
    // In flight the visible label must NOT swap (billing-disabled deployments
    // reject in milliseconds → a swap reads as a flicker). Busy state = spinner
    // + disabled + aria-busy + the live region announcing "opening".
    expect(screen.getByText("planPage.reviewOrChangePlan")).toBeInTheDocument();
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(liveRegion).toHaveTextContent("planPage.opening");
    // The visible affordance is the spinner, not a text swap: assert the
    // Sparkles icon was replaced by the spinner (no lucide-sparkles present).
    expect(button.querySelector(".lucide-sparkles")).toBeNull();

    resolveHandoff({});
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(button).toHaveAttribute("aria-busy", "false");
    expect(liveRegion).toHaveTextContent("");
  });

  it("labels the usage block as current entitlements next to the create matrix (#1560)", async () => {
    mockWorkspace = { current_user_role: "owner", plan_name: "basic" };
    render(<WorkspacePlanPage />);
    // Effective view (may serve on this tier) vs the matrix's create view —
    // both sections say which one they are so a non-zero effective
    // public-calls figure beside a ✗ matrix row does not read as a conflict.
    expect(
      await screen.findByText("planPage.usageDescription"),
    ).toBeInTheDocument();
    expect(screen.getByText("planPage.publicPerDay")).toBeInTheDocument();
    expect(screen.getByText("planMatrix.description")).toBeInTheDocument();
  });

  it("non-owner sees the owner-only note and no billing button", async () => {
    mockWorkspace = { current_user_role: "member", plan_name: "basic" };
    render(<WorkspacePlanPage />);
    expect(await screen.findByText("planPage.ownerOnly")).toBeInTheDocument();
    expect(screen.queryByText("planPage.reviewOrChangePlan")).toBeNull();
    expect(screen.queryByText("planPage.manageBilling")).toBeNull();
  });
});

describe("WorkspacePlanPage deployment notice (#1646 D3)", () => {
  it.each([
    ["ENABLE_PLAN_PAGE off", { plan_page: false }],
    ["a failed /system/info (fail closed)", {}],
  ] as const)(
    "%s: the notice sits inside the page's own header",
    async (_label, info) => {
      mockFeatures = { ...info };
      mockWorkspace = { current_user_role: "owner", plan_name: "basic" };
      render(<WorkspacePlanPage />);

      expect(
        await screen.findByRole("heading", { name: "planPage.title" }),
      ).toBeInTheDocument();
      expect(screen.getByText("planPage.description")).toBeInTheDocument();
      expect(screen.getByText("deployment.title")).toBeInTheDocument();
      expect(screen.getByText("deployment.description")).toBeInTheDocument();
      expect(mockGetWorkspacePlan).not.toHaveBeenCalled();
    },
  );

  it.each(["owner", "admin", "member"])(
    "never offers a CTA — the Plan page cannot send you to the Plan page (%s)",
    async (role) => {
      mockFeatures = { plan_page: false };
      mockWorkspace = { current_user_role: role, plan_name: "free" };
      render(<WorkspacePlanPage />);

      await screen.findByText("deployment.title");
      expect(screen.queryAllByRole("button")).toHaveLength(0);
      expect(screen.queryByText(/\.action$/)).toBeNull();
      expect(mockPush).not.toHaveBeenCalled();
    },
  );

  it("keeps the spinner while /system/info is pending, and fetches nothing", () => {
    mockFeatures = null;
    mockWorkspace = { current_user_role: "owner", plan_name: "basic" };
    render(<WorkspacePlanPage />);

    expect(screen.getByText("loading")).toBeInTheDocument();
    expect(screen.queryByText("deployment.title")).toBeNull();
    expect(mockGetWorkspacePlan).not.toHaveBeenCalled();
  });

  it("a non-owner on an enabled page falls through to today's flow, not a role notice", async () => {
    mockWorkspace = { current_user_role: "admin", plan_name: "basic" };
    render(<WorkspacePlanPage />);

    expect(await screen.findByText("planPage.ownerOnly")).toBeInTheDocument();
    expect(screen.queryByText("role.owner.title")).toBeNull();
    expect(screen.queryByText("deployment.title")).toBeNull();
  });
});
