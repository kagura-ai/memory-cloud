/**
 * WorkspaceQuotaDetail (#663, #712; moved out of the Quotas page in #2765):
 * the per-resource quota table of one workspace (base, add-on, usage /
 * effective) with its Edit add-ons and Edit spend cap actions.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceQuotaDetail } from "./WorkspaceQuotaDetail";
import { QUOTA_DETAIL_PRO } from "./plans.fixtures";

const mockGetWorkspaceQuotas = vi.fn();
vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    getWorkspaceQuotas: (...args: unknown[]) => mockGetWorkspaceQuotas(...args),
  };
});

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) => (key: string) =>
    namespace ? `${namespace}.${key}` : key,
  useLocale: () => "en",
}));

// The dialogs have their own tests; here only "which one opens" matters.
vi.mock("./AddonEditDialog", () => ({
  AddonEditDialog: ({
    quotaDetail,
  }: {
    quotaDetail: { workspace_id: string };
  }) => (
    <div data-testid="addon-dialog" data-workspace={quotaDetail.workspace_id} />
  ),
}));
vi.mock("./SpendCapEditDialog", () => ({
  SpendCapEditDialog: ({
    open,
    workspaceId,
  }: {
    open: boolean;
    workspaceId: string;
  }) =>
    open ? (
      <div data-testid="spend-cap-dialog" data-workspace={workspaceId} />
    ) : null,
}));

const SPEND_CAP = {
  tier_default_daily_usd: 10,
  tier_default_monthly_usd: 100,
  override_daily_usd: null,
  override_monthly_usd: null,
  effective_daily_usd: 10,
  effective_monthly_usd: 100,
  current_daily_usd: 1,
  current_monthly_usd: 5,
};

beforeEach(() => {
  mockGetWorkspaceQuotas.mockReset();
  mockGetWorkspaceQuotas.mockResolvedValue(QUOTA_DETAIL_PRO);
});

describe("WorkspaceQuotaDetail", () => {
  it("loads the workspace's quotas and renders usage / effective", async () => {
    render(<WorkspaceQuotaDetail workspaceId="ws-pro" />);

    const memoryRow = (
      await screen.findByText("admin.plans.quota.memory")
    ).closest("div.grid");
    expect(mockGetWorkspaceQuotas).toHaveBeenCalledWith("ws-pro");
    // usage (50,000) is the numerator, effective (110,000) the denominator.
    expect(memoryRow).toHaveTextContent("50,000 / 110,000");
  });

  it("renders the read-only max_resource_tokens row after the addon rows", async () => {
    render(<WorkspaceQuotaDetail workspaceId="ws-pro" />);
    await screen.findByText("admin.plans.quota.maxResourceTokens");
    expect(screen.getByText("admin.plans.quota.storage")).toBeInTheDocument();
    expect(screen.getByText("admin.plans.quota.sleep")).toBeInTheDocument();
  });

  it("opens the add-on dialog for this workspace", async () => {
    render(<WorkspaceQuotaDetail workspaceId="ws-pro" />);
    fireEvent.click(await screen.findByText("admin.plans.actions.editAddons"));
    expect(screen.getByTestId("addon-dialog")).toHaveAttribute(
      "data-workspace",
      "ws-pro",
    );
  });

  it("offers Edit spend cap only when the workspace has a spend cap", async () => {
    const { unmount } = render(<WorkspaceQuotaDetail workspaceId="ws-pro" />);
    await screen.findByText("admin.plans.actions.editAddons");
    expect(screen.queryByText("admin.plans.actions.editSpendCap")).toBeNull();
    unmount();

    mockGetWorkspaceQuotas.mockResolvedValue({
      ...QUOTA_DETAIL_PRO,
      spend_cap: SPEND_CAP,
    });
    render(<WorkspaceQuotaDetail workspaceId="ws-pro" />);
    fireEvent.click(
      await screen.findByText("admin.plans.actions.editSpendCap"),
    );
    expect(screen.getByTestId("spend-cap-dialog")).toHaveAttribute(
      "data-workspace",
      "ws-pro",
    );
  });

  it("refetches when the parent bumps refreshKey (after a plan change)", async () => {
    const { rerender } = render(
      <WorkspaceQuotaDetail workspaceId="ws-pro" refreshKey={0} />,
    );
    await screen.findByText("admin.plans.quota.memory");
    rerender(<WorkspaceQuotaDetail workspaceId="ws-pro" refreshKey={1} />);
    await waitFor(() =>
      expect(mockGetWorkspaceQuotas).toHaveBeenCalledTimes(2),
    );
  });

  it("renders the error banner when the quotas fail to load", async () => {
    mockGetWorkspaceQuotas.mockRejectedValue(new Error("boom"));
    render(<WorkspaceQuotaDetail workspaceId="ws-pro" />);
    expect(
      await screen.findByText("admin.plans.quota.loadError"),
    ).toBeInTheDocument();
    expect(screen.queryByText("admin.plans.actions.editAddons")).toBeNull();
  });
});
