/**
 * Tests for SpendCapEditDialog (Issue #712, #709 follow-up).
 *
 * Focus is the null / 0 / positive input mapping that the CDO gate1 review
 * flagged as the main footgun: empty -> null (clear override), explicit 0 ->
 * lockout (warned but allowed), positive -> override. Plus the tier-ceiling
 * 400 rejection surfacing as an in-dialog Alert (frontend.md error-surface).
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SpendCapEditDialog } from "./SpendCapEditDialog";
import type { SpendCapValues } from "@/lib/api/admin";

const mockUpdateWorkspaceSpendCap = vi.fn();

vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    updateWorkspaceSpendCap: (...args: unknown[]) =>
      mockUpdateWorkspaceSpendCap(...args),
  };
});

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// Mirror the next-intl mock used by page.test.tsx: a translator returns
// "<namespace>.<key>" so assertions can target stable key strings.
vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) => (key: string) =>
    namespace ? `${namespace}.${key}` : key,
  useLocale: () => "en",
}));

const withOverride: SpendCapValues = {
  tier_default_daily_usd: 10,
  tier_default_monthly_usd: 200,
  override_daily_usd: 5,
  override_monthly_usd: 100,
  effective_daily_usd: 5,
  effective_monthly_usd: 100,
  current_daily_usd: 2,
  current_monthly_usd: 40,
};

const noOverride: SpendCapValues = {
  tier_default_daily_usd: 10,
  tier_default_monthly_usd: 200,
  override_daily_usd: null,
  override_monthly_usd: null,
  effective_daily_usd: 10,
  effective_monthly_usd: 200,
  current_daily_usd: 1,
  current_monthly_usd: 20,
};

function renderDialog(spendCap: SpendCapValues) {
  const onOpenChange = vi.fn();
  const onSaved = vi.fn();
  render(
    <SpendCapEditDialog
      open
      onOpenChange={onOpenChange}
      workspaceId="ws-1"
      workspaceName="Acme"
      spendCap={spendCap}
      onSaved={onSaved}
    />,
  );
  return { onOpenChange, onSaved };
}

const dailyInput = () =>
  document.getElementById("spend-cap-daily") as HTMLInputElement;
const monthlyInput = () =>
  document.getElementById("spend-cap-monthly") as HTMLInputElement;
const clickSave = () =>
  fireEvent.click(screen.getByText("admin.plans.spendCapDialog.save"));

beforeEach(() => {
  mockUpdateWorkspaceSpendCap.mockReset();
  mockUpdateWorkspaceSpendCap.mockResolvedValue({ message: "ok" });
});

describe("SpendCapEditDialog", () => {
  it("prefills both inputs from the override values when an override is set", () => {
    renderDialog(withOverride);
    expect(dailyInput().value).toBe("5");
    expect(monthlyInput().value).toBe("100");
  });

  it("renders empty inputs (inherit tier default) when no override is set", () => {
    renderDialog(noOverride);
    expect(dailyInput().value).toBe("");
    expect(monthlyInput().value).toBe("");
    // tier default is surfaced as the placeholder so empty reads as "inherit".
    expect(dailyInput().placeholder).toBe("$10.00");
    expect(monthlyInput().placeholder).toBe("$200.00");
  });

  it("submits positive values as the override payload", async () => {
    renderDialog(withOverride);
    fireEvent.change(dailyInput(), { target: { value: "7" } });
    clickSave();

    await waitFor(() =>
      expect(mockUpdateWorkspaceSpendCap).toHaveBeenCalledTimes(1),
    );
    const [workspaceId, body] = mockUpdateWorkspaceSpendCap.mock.calls[0];
    expect(workspaceId).toBe("ws-1");
    expect(body).toEqual({
      embedding_daily_cap_usd: 7,
      embedding_monthly_cap_usd: 100,
    });
  });

  it("submits null for emptied inputs (clears the override)", async () => {
    const { onOpenChange, onSaved } = renderDialog(withOverride);
    fireEvent.change(dailyInput(), { target: { value: "" } });
    fireEvent.change(monthlyInput(), { target: { value: "" } });
    clickSave();

    await waitFor(() =>
      expect(mockUpdateWorkspaceSpendCap).toHaveBeenCalledTimes(1),
    );
    expect(mockUpdateWorkspaceSpendCap.mock.calls[0][1]).toEqual({
      embedding_daily_cap_usd: null,
      embedding_monthly_cap_usd: null,
    });
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(onSaved).toHaveBeenCalledTimes(1);
  });

  it("warns on a 0 (lockout) value but still allows saving it", async () => {
    renderDialog(withOverride);
    fireEvent.change(dailyInput(), { target: { value: "0" } });

    expect(
      screen.getByText("admin.plans.spendCapDialog.lockoutWarning.title"),
    ).toBeInTheDocument();

    clickSave();
    await waitFor(() =>
      expect(mockUpdateWorkspaceSpendCap).toHaveBeenCalledTimes(1),
    );
    expect(mockUpdateWorkspaceSpendCap.mock.calls[0][1]).toEqual({
      embedding_daily_cap_usd: 0,
      embedding_monthly_cap_usd: 100,
    });
  });

  it("blocks save with an inline field error for a negative value", () => {
    renderDialog(withOverride);
    fireEvent.change(dailyInput(), { target: { value: "-5" } });

    expect(
      screen.getByText("admin.plans.spendCapDialog.negativeNotAllowed"),
    ).toBeInTheDocument();
    const saveButton = screen
      .getByText("admin.plans.spendCapDialog.save")
      .closest("button");
    expect(saveButton).toBeDisabled();

    // A disabled button does not invoke onClick, so the API is never hit.
    fireEvent.click(saveButton!);
    expect(mockUpdateWorkspaceSpendCap).not.toHaveBeenCalled();
  });

  it("surfaces a tier-ceiling 400 rejection as an in-dialog error and keeps the dialog open", async () => {
    mockUpdateWorkspaceSpendCap.mockRejectedValueOnce(
      new Error("daily cap exceeds tier default"),
    );
    const { onOpenChange, onSaved } = renderDialog(withOverride);
    fireEvent.change(dailyInput(), { target: { value: "9999" } });
    clickSave();

    expect(
      await screen.findByText("daily cap exceeds tier default"),
    ).toBeInTheDocument();
    // The dialog must not close and must not signal a successful save.
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(onSaved).not.toHaveBeenCalled();
  });
});
