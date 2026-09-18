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
  mockCurrentWorkspace = { plan_name: "promax", current_user_role: "owner" };
});

const createButton = () => screen.getByRole("button", { name: /createToken/ });

describe("ResourceTokensTabPanel — XL-only create gate (#1551)", () => {
  it.each([["basic"], ["pro"]])(
    "%s: existing tokens stay listed, Create is disabled behind the XL upsell",
    async (plan) => {
      mockCurrentWorkspace = { plan_name: plan, current_user_role: "owner" };
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
});
