/**
 * CreateResourceTokenDialog — quota bounds from the plan API (#1560).
 *
 * The plan's total capacity arrives as the `maxQuotaCapacity` prop (from
 * `GET /workspaces/{id}/plan` via the owning panel) instead of a per-tier
 * table keyed by plan name. `null` = unknown: only the per-token ceiling
 * binds client-side and the hint says so.
 *
 * #1646 Q5: a used-up capacity is the gate's quota `control` hint (the
 * relative gate key under the key-echo mock), not a hand-coloured span.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
// FeatureGateNotice routes its CTA through the app router.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
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

function dialogProps(
  maxQuotaCapacity: number | null,
  currentTokens: ReturnType<typeof activeToken>[] = [],
) {
  return {
    isOpen: true,
    onClose: noop,
    onSuccess: noop,
    currentTokens,
    maxQuotaCapacity,
  };
}

function renderDialog(
  maxQuotaCapacity: number | null,
  currentTokens: ReturnType<typeof activeToken>[] = [],
) {
  return render(
    <CreateResourceTokenDialog
      {...dialogProps(maxQuotaCapacity, currentTokens)}
    />,
  );
}

const quotaInput = () =>
  screen.getByLabelText("createDialog.quota") as HTMLInputElement;

const submitForm = () =>
  fireEvent.submit(quotaInput().closest("form") as HTMLFormElement);

// A resource context to pick, so Create is not disabled for lack of one.
const withResourceContext = () =>
  mockGetContexts.mockResolvedValue({
    contexts: [{ id: "ctx-1", name: "products", resource_id: "products" }],
  });
const createButton = () =>
  screen.getByRole("button", { name: "createDialog.create" });

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

  it("says the plan is exhausted when the remaining capacity is zero", async () => {
    withResourceContext();
    renderDialog(20000, [activeToken(1, 10000), activeToken(2, 10000)]);
    // The resource picker has loaded, so only the capacity can disable Create.
    await screen.findByRole("combobox");

    expect(quotaInput().value).toBe("0");
    // #1646 Q5: the gate's quota hint, hint only — no badge repeating it.
    const hint = screen.getByText("quota.hint");
    expect(screen.queryByText("quota.badge")).toBeNull();
    expect(screen.queryByText("createDialog.quotaRemaining")).toBeNull();
    // The hint describes the quota field and the disabled Create button.
    expect(hint.id).not.toBe("");
    expect(quotaInput()).toHaveAttribute("aria-describedby", hint.id);
    expect(createButton()).toHaveAttribute("aria-describedby", hint.id);
    expect(createButton()).toBeDisabled();
    // No tier can be named for an events/hour capacity: no upgrade CTA.
    expect(screen.queryByRole("button", { name: "quota.action" })).toBeNull();
  });

  it("does not block while there is capacity left (#1646)", async () => {
    withResourceContext();
    renderDialog(20000, [activeToken(1, 10000), activeToken(2, 9999)]);

    // Enabled once the resource context has loaded: capacity is not a block.
    await waitFor(() => expect(createButton()).toBeEnabled());
    expect(createButton()).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText("quota.hint")).toBeNull();
    expect(screen.getByText("createDialog.quotaRemaining")).toBeInTheDocument();
    expect(quotaInput()).not.toHaveAttribute("aria-describedby");
  });

  it("an unknown or zero capacity never reads as the limit reached (#1646)", () => {
    const { unmount } = renderDialog(null, [activeToken(1, 10000)]);
    expect(screen.queryByText("quota.hint")).toBeNull();
    unmount();

    renderDialog(0);
    expect(screen.queryByText("quota.hint")).toBeNull();
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

  // The owning panel's /plan fetch is async, so the dialog can mount on
  // `null` (per-token ceiling) and learn a smaller remaining capacity a
  // moment later. The seeded default must follow the bound, or submit would
  // reject the very value the dialog pre-filled.
  it("re-seeds the default when the plan bound arrives after mount", () => {
    const tokens = [activeToken(1, 299000)];
    const { rerender } = renderDialog(null, tokens);
    expect(quotaInput().value).toBe(String(MAX_QUOTA_PER_TOKEN));

    rerender(<CreateResourceTokenDialog {...dialogProps(300000, tokens)} />);

    expect(quotaInput()).toHaveAttribute("max", "1000");
    expect(quotaInput().value).toBe("1000");
  });

  it("keeps a value the user already typed when the bound moves", () => {
    const tokens = [activeToken(1, 299000)];
    const { rerender } = renderDialog(null, tokens);
    fireEvent.change(quotaInput(), { target: { value: "500" } });

    rerender(<CreateResourceTokenDialog {...dialogProps(300000, tokens)} />);

    expect(quotaInput()).toHaveAttribute("max", "1000");
    expect(quotaInput().value).toBe("500");
  });

  // Validation copy goes through next-intl like every other string in the
  // dialog (the mock renders the key), with the remaining figure only when
  // the plan total is known.
  it("reports an out-of-range quota via i18n, naming the remaining capacity when known", () => {
    render(
      <CreateResourceTokenDialog
        {...dialogProps(300000, [activeToken(1, 299000)])}
        initialResourceId="products"
      />,
    );
    fireEvent.change(quotaInput(), { target: { value: "5000" } });
    submitForm();

    expect(
      screen.getByText("createDialog.quotaRangeErrorRemaining"),
    ).toBeInTheDocument();
  });

  it("reports an out-of-range quota via i18n without a remaining figure when the plan total is unknown", () => {
    render(
      <CreateResourceTokenDialog
        {...dialogProps(null)}
        initialResourceId="products"
      />,
    );
    fireEvent.change(quotaInput(), {
      target: { value: String(MAX_QUOTA_PER_TOKEN + 1) },
    });
    submitForm();

    expect(
      screen.getByText("createDialog.quotaRangeError"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("createDialog.quotaRangeErrorRemaining"),
    ).toBeNull();
  });
});
