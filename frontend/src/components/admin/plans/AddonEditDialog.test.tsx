/**
 * AddonEditDialog (#663, #800, #1561; moved out of the Quotas page in #2765).
 *
 * The request body is pinned exactly as the Quotas page sent it: every
 * addon_* field, absolute values, one PUT per save.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AddonEditDialog } from "./AddonEditDialog";
import { QUOTA_DETAIL_PRO } from "./plans.fixtures";
import type { WorkspaceQuotaDetail } from "@/lib/api/admin";

const mockUpdateWorkspaceAddons = vi.fn();
vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    updateWorkspaceAddons: (...args: unknown[]) =>
      mockUpdateWorkspaceAddons(...args),
  };
});

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) => (key: string) =>
    namespace ? `${namespace}.${key}` : key,
  useLocale: () => "en",
}));

const detail = QUOTA_DETAIL_PRO as unknown as WorkspaceQuotaDetail;

function renderDialog(quotaDetail: WorkspaceQuotaDetail = detail) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  render(
    <AddonEditDialog
      quotaDetail={quotaDetail}
      onClose={onClose}
      onSaved={onSaved}
    />,
  );
  return { onClose, onSaved };
}

beforeEach(() => {
  mockUpdateWorkspaceAddons.mockReset();
  mockUpdateWorkspaceAddons.mockResolvedValue(undefined);
  mockToast.mockReset();
});

describe("AddonEditDialog", () => {
  it("renders every addon input pre-populated from the quota detail", () => {
    renderDialog();
    expect(
      screen.getByText("admin.plans.addonDialog.title"),
    ).toBeInTheDocument();
    for (const id of [
      "addon-memory",
      "addon-mcp",
      "addon-rest",
      "addon-public",
      "addon-members",
      "addon-contexts",
      "addon-analysis",
      "addon-storage",
      "addon-sleep",
      "addon-connector",
    ]) {
      const input = document.getElementById(id) as HTMLInputElement | null;
      expect(input).not.toBeNull();
      expect(input!.tagName).toBe("INPUT");
    }
    expect(
      (document.getElementById("addon-memory") as HTMLInputElement).value,
    ).toBe("10000");
  });

  it("sends every addon field as an absolute value, as the Quotas page did", async () => {
    const { onSaved } = renderDialog();
    fireEvent.change(
      document.getElementById("addon-memory") as HTMLInputElement,
      {
        target: { value: "20000" },
      },
    );
    fireEvent.click(screen.getByText("admin.plans.addonDialog.save"));

    await waitFor(() =>
      expect(mockUpdateWorkspaceAddons).toHaveBeenCalledTimes(1),
    );
    const [workspaceId, body] = mockUpdateWorkspaceAddons.mock.calls[0];
    expect(workspaceId).toBe("ws-pro");
    expect(body).toEqual({
      addon_memory_bonus: 20_000,
      addon_mcp_quota_bonus: 0,
      addon_rest_quota_bonus: 0,
      addon_public_quota_bonus: 0,
      addon_member_bonus: 0,
      addon_context_bonus: 0,
      addon_analysis_bonus: 0,
      addon_storage_bonus_mb: 0,
      addon_sleep_contexts_bonus: 0,
      addon_connector_bonus: 0,
      addon_memories_per_day_bonus: 0,
      addon_history_retention_days_bonus: 0,
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
  });

  it("renders the PRO-only inline note exactly once (on the sleep addon)", () => {
    renderDialog();
    expect(
      screen.getAllByText("admin.plans.addonDialog.proOnlyInline"),
    ).toHaveLength(1);
  });

  it("warns, preserves the value, and disables Save for a non-multiple cache (#800)", () => {
    renderDialog({
      ...detail,
      addon: { ...detail.addon, memory_bonus: 9000 },
    });
    expect(
      screen.getByText("admin.plans.addonDialog.invalidWarning.title"),
    ).toBeInTheDocument();
    expect(
      (document.getElementById("addon-memory") as HTMLInputElement).value,
    ).toBe("9000");
    expect(
      screen.getByText("admin.plans.addonDialog.save").closest("button"),
    ).toBeDisabled();
    expect(
      screen.queryByText("admin.plans.addonDialog.reductionWarning"),
    ).not.toBeInTheDocument();
  });

  it("clears the warning and re-enables Save once a valid value is entered (#800)", () => {
    renderDialog({
      ...detail,
      addon: { ...detail.addon, memory_bonus: 9000 },
    });
    fireEvent.change(
      document.getElementById("addon-memory") as HTMLInputElement,
      {
        target: { value: "20000" },
      },
    );
    expect(
      screen.queryByText("admin.plans.addonDialog.invalidWarning.title"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("admin.plans.addonDialog.save").closest("button"),
    ).not.toBeDisabled();
  });

  it("shows the reduction warning when a counted addon is lowered", () => {
    renderDialog();
    fireEvent.change(
      document.getElementById("addon-memory") as HTMLInputElement,
      {
        target: { value: "0" },
      },
    );
    expect(
      screen.getByText("admin.plans.addonDialog.reductionWarning"),
    ).toBeInTheDocument();
  });

  it("shows the backend feature warning and keeps the dialog open (#1561)", async () => {
    mockUpdateWorkspaceAddons.mockResolvedValue({
      message: "ok",
      warnings: ["connectors_feature_missing"],
    });
    const { onClose } = renderDialog();
    fireEvent.click(screen.getByText("admin.plans.addonDialog.save"));

    await screen.findByText(
      "admin.plans.addonDialog.featureWarning.connectorsFeatureMissing",
    );
    expect(
      screen.getByText("admin.plans.addonDialog.featureWarning.title"),
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes when the backend returns no warning (#1561)", async () => {
    mockUpdateWorkspaceAddons.mockResolvedValue({
      message: "ok",
      warnings: [],
    });
    const { onClose } = renderDialog();
    fireEvent.click(screen.getByText("admin.plans.addonDialog.save"));
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(
      screen.queryByText("admin.plans.addonDialog.featureWarning.title"),
    ).not.toBeInTheDocument();
  });

  it("toasts the server message when the save fails", async () => {
    mockUpdateWorkspaceAddons.mockRejectedValue(
      new Error("below current usage"),
    );
    const { onClose, onSaved } = renderDialog();
    fireEvent.click(screen.getByText("admin.plans.addonDialog.save"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: "below current usage",
          variant: "destructive",
        }),
      ),
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });
});
