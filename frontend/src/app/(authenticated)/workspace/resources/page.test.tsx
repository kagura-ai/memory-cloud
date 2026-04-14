/**
 * Tests for the Resources list page.
 *
 * Verifies:
 * - table rows render from listResources() response
 * - empty-state renders when the response is empty
 * - plan-gated workspaces see the upgrade CTA and never fire the fetch
 * - the fetch is held until WorkspaceContext hydrates (no flash for free/basic)
 * - errors render via ErrorBanner, not toast
 * - row click navigates to detail page
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

let mockCurrentWorkspace: { plan_name?: string } | null = null;

vi.mock("@/lib/api/resources", () => ({
  listResources: (...args: unknown[]) => mockListResources(...args),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspace: mockCurrentWorkspace }),
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
  mockCurrentWorkspace = { plan_name: "pro" };
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
    const link = screen.getByRole("link", { name: /list\.setupGuide/ });
    expect(link.getAttribute("href")).toContain("kagura-ai/memory-cloud");
  });

  it("renders upgrade CTA and skips fetch on basic plan", async () => {
    mockCurrentWorkspace = { plan_name: "basic" };

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("planGate.title")).toBeInTheDocument();
    });
    expect(mockListResources).not.toHaveBeenCalled();
  });

  it("upgrade CTA button navigates to billing", async () => {
    mockCurrentWorkspace = { plan_name: "free" };

    render(<ResourcesListPage />);

    await waitFor(() => {
      expect(screen.getByText("planGate.title")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "planGate.action" }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/billing");
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
});
