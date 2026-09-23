/**
 * Tests for the Resources list page.
 *
 * Verifies:
 * - table rows render from listResources() response
 * - empty-state renders when the response is empty
 * - below XL the list still renders (existing resources keep serving, #1551)
 *   with an upsell banner naming the XL tier; XL shows no banner
 * - the fetch is held until WorkspaceContext hydrates
 * - errors render via ErrorBanner, not toast
 * - row click navigates to detail page
 * - #1643: the upsell banner keeps its copy but drops the button when the
 *   Plan page is not reachable on this deployment
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  cleanup,
} from "@testing-library/react";

import ResourcesListPage from "./page";
import type { ResourceListItem } from "@/lib/api/resources";

// ---------- Mocks ------------------------------------------------------------

const mockListResources = vi.fn();
const mockPush = vi.fn();

let mockCurrentWorkspace: {
  plan_name?: string;
  current_user_role?: string;
} | null = null;

vi.mock("@/lib/api/resources", () => ({
  listResources: (...args: unknown[]) => mockListResources(...args),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

// Stable translator — a fresh function on every render invalidates
// useCallback([t]) in the component and turns an effect that depends on
// that callback into a tight re-fetch loop (masquerades as flaky tests).
// Cache per-namespace in module scope, same idea as the detail page test.
const translatorCache = new Map<string, (k: string) => string>();
vi.mock("next-intl", () => ({
  useTranslations: (ns?: string) => {
    const key = ns ?? "";
    if (!translatorCache.has(key)) {
      translatorCache.set(key, (k: string) => k);
    }
    return translatorCache.get(key)!;
  },
  useLocale: () => "en",
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspace: mockCurrentWorkspace }),
}));

// #1643: useCanUpgrade reads /system/info. Without this mock the real hook
// fires a jsdom fetch, retries three times and leaves a module-level cache
// that leaks between cases in this file. `null` = still resolving.
let mockFeatures: Record<string, boolean> | null = { plan_page: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

// #1560: the create gate is the tier matrix's `resources` boolean via
// usePlanFeature (tri-state; `null` = resolving), not a tier-name rank.
let mockPlanFeature: boolean | null = true;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanFeature: () => mockPlanFeature,
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: (iso: string) => `rel(${iso})`,
}));

// ---------- Fixtures ---------------------------------------------------------

const item = (overrides: Partial<ResourceListItem> = {}): ResourceListItem => ({
  resource_id: "ec_products",
  context_id: "550e8400-e29b-41d4-a716-446655440000",
  context_name: "ec-products",
  context_display_name: "EC Products",
  token_count: 2,
  memory_count: 47,
  current_schema_version: 3,
  created_at: "2026-03-01T00:00:00Z",
  updated_at: "2026-04-14T09:15:30Z",
  ...overrides,
});

beforeEach(() => {
  mockListResources.mockReset();
  mockPush.mockReset();
  // #1551: resources are XL-only to create — promax is the "no upsell" tier.
  mockCurrentWorkspace = { plan_name: "promax", current_user_role: "owner" };
  mockPlanFeature = true; // #1560: resources included unless a test says otherwise
  mockFeatures = { plan_page: true };
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("ResourcesListPage", () => {
  it("renders table rows from listResources()", async () => {
    mockListResources.mockResolvedValue({
      resources: [
        item(),
        item({
          resource_id: "other",
          context_display_name: "Other",
          current_schema_version: 5,
        }),
      ],
      total: 2,
    });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("ec_products")).toBeInTheDocument();
    });
    expect(screen.getByText("EC Products")).toBeInTheDocument();
    expect(screen.getByText("other")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    expect(screen.getByText("v5")).toBeInTheDocument();
  });

  it("renders empty state when the list is empty", async () => {
    mockListResources.mockResolvedValue({ resources: [], total: 0 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("list.emptyTitle")).toBeInTheDocument();
    });
    expect(screen.getByText("list.emptyDescription")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /setupGuide/i })).toBeNull();
  });

  it.each([["basic"], ["pro"]])(
    "%s: renders existing resources (may serve) and the XL upsell banner (#1551)",
    async (plan) => {
      mockCurrentWorkspace = { plan_name: plan, current_user_role: "owner" };
      mockPlanFeature = false; // #1560: the matrix says resources=false here
      mockListResources.mockResolvedValue({ resources: [item()], total: 1 });

      render(<ResourcesListPage />);

      await waitFor(() => {
        expect(screen.getByText("ec_products")).toBeInTheDocument();
      });
      expect(mockListResources).toHaveBeenCalledTimes(1);
      // Block-new-only: the list is served, creation is what the banner gates.
      expect(screen.getByText("planGate.title")).toBeInTheDocument();
    },
  );

  // #1560: the banner follows the API boolean, not the tier's name/rank.
  it("pro with resources=true from the matrix: list served, no upsell (#1560)", async () => {
    mockCurrentWorkspace = { plan_name: "pro", current_user_role: "owner" };
    mockPlanFeature = true;
    mockListResources.mockResolvedValue({ resources: [item()], total: 1 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("ec_products")).toBeInTheDocument();
    });
    expect(screen.queryByText("planGate.title")).toBeNull();
  });

  it("pending gate: list served, no upsell flash while the matrix resolves (#1560)", async () => {
    mockCurrentWorkspace = { plan_name: "basic", current_user_role: "owner" };
    mockPlanFeature = null;
    mockListResources.mockResolvedValue({ resources: [item()], total: 1 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("ec_products")).toBeInTheDocument();
    });
    expect(screen.queryByText("planGate.title")).toBeNull();
  });

  it("promax is not plan-gated: fetches and renders, no upgrade CTA (#1548)", async () => {
    mockCurrentWorkspace = { plan_name: "promax", current_user_role: "owner" };
    mockListResources.mockResolvedValue({ resources: [item()], total: 1 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("ec_products")).toBeInTheDocument();
    });
    expect(mockListResources).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("planGate.title")).toBeNull();
  });

  it("promax is not plan-gated: fetches and renders, no upgrade CTA (#1548)", async () => {
    mockCurrentWorkspace = { plan_name: "promax", current_user_role: "owner" };
    mockListResources.mockResolvedValue({ resources: [item()], total: 1 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("ec_products")).toBeInTheDocument();
    });
    expect(mockListResources).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("planGate.title")).toBeNull();
  });

  it("upgrade CTA button navigates to the plan page", async () => {
    mockCurrentWorkspace = { plan_name: "free", current_user_role: "owner" };
    mockPlanFeature = false;
    mockListResources.mockResolvedValue({ resources: [], total: 0 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("planGate.title")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "planGate.action" }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
  });

  it("plan-gate banner keeps its copy and drops the button when plan_page is off", async () => {
    mockCurrentWorkspace = { plan_name: "free", current_user_role: "owner" };
    mockPlanFeature = false;
    mockFeatures = {};
    mockListResources.mockResolvedValue({ resources: [], total: 0 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("planGate.title")).toBeInTheDocument();
    });
    // The explanation is the point of the banner — it stays.
    expect(screen.getByText("planGate.description")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "planGate.action" }),
    ).toBeNull();
  });

  it("plan-gate banner drops the button while /system/info is pending", async () => {
    mockCurrentWorkspace = { plan_name: "free", current_user_role: "owner" };
    mockPlanFeature = false;
    mockFeatures = null;
    mockListResources.mockResolvedValue({ resources: [], total: 0 });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("planGate.title")).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("button", { name: "planGate.action" }),
    ).toBeNull();
  });

  it("holds the fetch until WorkspaceContext hydrates", async () => {
    mockCurrentWorkspace = null;
    mockListResources.mockResolvedValue({ resources: [], total: 0 });

    render(<ResourcesListPage />);

    // Yield microtasks — should still be pending because workspace is null
    await Promise.resolve();
    expect(mockListResources).not.toHaveBeenCalled();
  });

  it("renders ErrorBanner when fetch rejects", async () => {
    mockListResources.mockRejectedValue(new Error("backend offline"));

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("backend offline");
    });
  });

  it("does not render the EmptyState 'No resources yet' copy on fetch error", async () => {
    // Regression: resources=[] + error set used to render the ErrorBanner
    // alongside the "No resources yet / Set one up" EmptyState, which
    // misleadingly implied the workspace was empty rather than that the
    // fetch had failed.
    mockListResources.mockRejectedValue(new Error("backend offline"));

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
    expect(screen.queryByText("list.emptyTitle")).not.toBeInTheDocument();
  });

  it("renders an accessible Link per row for navigation (not a row click handler)", async () => {
    mockListResources.mockResolvedValue({
      resources: [item({ resource_id: "foo_bar" })],
      total: 1,
    });

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("foo_bar")).toBeInTheDocument();
    });
    // Anchor preserves browser affordances (open-in-new-tab, copy-link, etc.)
    // that a row-level onClick cannot. role=link is the expected a11y semantic.
    const link = screen.getByRole("link", { name: "foo_bar" });
    expect(link).toHaveAttribute("href", "/workspace/resources/foo_bar");
  });

  // Issue #389: owner-only access. Non-owner roles silently redirect to the
  // dashboard before any API call fires, matching the #365/#381 UX.
  it.each([["admin"], ["member"], ["viewer"]])(
    "redirects to /workspace/dashboard for role=%s and does not fetch",
    async (role) => {
      mockCurrentWorkspace = { plan_name: "pro", current_user_role: role };

      render(<ResourcesListPage />);

      await waitFor(() => {
        expect(mockPush).toHaveBeenCalledWith("/workspace/dashboard");
      });
      expect(mockListResources).not.toHaveBeenCalled();
    },
  );
});
