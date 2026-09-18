/**
 * CreateResourceTokenDialog — quota bounds from the plan API (#1560).
 *
 * The plan's total capacity arrives as the `maxQuotaCapacity` prop (from
 * `GET /workspaces/{id}/plan` via the owning panel) instead of a per-tier
 * table keyed by plan name. `null` = unknown: only the per-token ceiling
 * binds client-side and the hint says so.
 */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CreateResourceTokenDialog } from "./CreateResourceTokenDialog";
import { MAX_QUOTA_PER_TOKEN } from "@/config/resource-tokens";

vi.mock("@/lib/api/resource-tokens", () => ({
  createResourceToken: vi.fn(),
}));
const mockGetContexts = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...args: unknown[]) => mockGetContexts(...args),
}));
vi.mock("@/lib/utils/clipboard", () => ({
  copyText: vi.fn(),
}));
vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

const activeToken = (id: number, quota: number) => ({
  id,
  resource_id: "products",
  description: null,
  quota_events_per_hour: quota,
  created_by: "owner-1",
  created_at: "2026-01-01T00:00:00Z",
  last_used_at: null,
  is_active: true,
  status: "active" as const,
});

const noop = () => {};

function renderDialog(
  maxQuotaCapacity: number | null,
  currentTokens: ReturnType<typeof activeToken>[] = [],
) {
  return render(
    <CreateResourceTokenDialog
      isOpen
      onClose={noop}
      onSuccess={noop}
      currentTokens={currentTokens}
      maxQuotaCapacity={maxQuotaCapacity}
    />,
  );
}

const quotaInput = () =>
  screen.getByLabelText("createDialog.quota") as HTMLInputElement;

beforeEach(() => {
  mockGetContexts.mockReset();
  mockGetContexts.mockResolvedValue({ contexts: [] });
});

describe("CreateResourceTokenDialog — quota bounds from the plan API (#1560)", () => {
  it("caps the token at min(plan remaining, per-token ceiling) when the plan total is known", () => {
    // 300,000 total, 299,000 already allocated → 1,000 left for this token.
    renderDialog(300000, [
      activeToken(1, MAX_QUOTA_PER_TOKEN * 29),
      activeToken(2, 9000),
    ]);

    expect(quotaInput().value).toBe("1000");
    expect(quotaInput()).toHaveAttribute("max", "1000");
    expect(screen.getByText("createDialog.quotaRemaining")).toBeInTheDocument();
  });

  it("says the plan is exhausted when the remaining capacity is zero", () => {
    renderDialog(20000, [activeToken(1, 10000), activeToken(2, 10000)]);

    expect(quotaInput().value).toBe("0");
    expect(
      screen.getByText("createDialog.quotaLimitReached"),
    ).toBeInTheDocument();
  });

  it("falls back to the per-token ceiling and says the plan total is unknown (null)", () => {
    renderDialog(null, [activeToken(1, 9000)]);

    expect(quotaInput().value).toBe(String(MAX_QUOTA_PER_TOKEN));
    expect(quotaInput()).toHaveAttribute("max", String(MAX_QUOTA_PER_TOKEN));
    expect(
      screen.getByText("createDialog.quotaCapUnknown"),
    ).toBeInTheDocument();
    expect(screen.queryByText("createDialog.quotaRemaining")).toBeNull();
  });
});
