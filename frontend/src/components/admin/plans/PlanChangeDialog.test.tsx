/**
 * PlanChangeDialog (#2765): the one plan change dialog, used on the admin
 * user detail. It sends what the Quotas page sent — `updateWorkspacePlan(id,
 * { plan_name })` — and shows the same warning.
 */

import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PLAN_TIER_ORDER } from "@/lib/utils/planLabel";

import { PlanChangeDialog } from "./PlanChangeDialog";

const mockUpdateWorkspacePlan = vi.fn();
vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    updateWorkspacePlan: (...args: unknown[]) =>
      mockUpdateWorkspacePlan(...args),
  };
});

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next-intl", () => ({
  useTranslations:
    (namespace: string) => (key: string, params?: Record<string, unknown>) =>
      `${namespace}.${key}${params ? ` ${JSON.stringify(params)}` : ""}`,
  useLocale: () => "en",
}));

// Radix Select needs pointer APIs jsdom lacks; a native <select> keeps the
// options inspectable.
type Children = { children: React.ReactNode };
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange?: (v: string) => void;
    children: React.ReactNode;
  }) => (
    <select
      data-testid="select-mock"
      value={value}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: Children) => <>{children}</>,
  SelectValue: () => null,
  SelectContent: ({ children }: Children) => <>{children}</>,
  SelectItem: ({
    value,
    children,
  }: {
    value: string;
    children: React.ReactNode;
  }) => <option value={value}>{children}</option>,
}));

const WORKSPACE = { id: "ws-pro", name: "Pro Workspace", planName: "pro" };

function renderDialog(workspace = WORKSPACE) {
  const onClose = vi.fn();
  const onChanged = vi.fn();
  render(
    <PlanChangeDialog
      workspace={workspace}
      onClose={onClose}
      onChanged={onChanged}
    />,
  );
  return { onClose, onChanged };
}

beforeEach(() => {
  mockUpdateWorkspacePlan.mockReset();
  mockUpdateWorkspacePlan.mockResolvedValue({ message: "ok" });
  mockToast.mockReset();
});

afterEach(() => {
  delete process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES;
});

describe("PlanChangeDialog", () => {
  it("offers one option per PLAN_TIER_ORDER entry with the default labels", () => {
    renderDialog();
    const options = within(screen.getByTestId("select-mock")).getAllByRole(
      "option",
    ) as HTMLOptionElement[];
    expect(options.map((o) => o.value)).toEqual([...PLAN_TIER_ORDER]);
    expect(options.map((o) => o.textContent)).toEqual(["S", "M", "L", "XL"]);
  });

  it("labels the options and the current plan with the configured display names (#1848)", () => {
    process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES = JSON.stringify({
      en: { free: "Free", basic: "Starter", pro: "Pro", promax: "Max" },
    });
    renderDialog({ ...WORKSPACE, planName: "basic" });
    const options = within(screen.getByTestId("select-mock")).getAllByRole(
      "option",
    ) as HTMLOptionElement[];
    expect(options.map((o) => o.textContent)).toEqual([
      "Free",
      "Starter",
      "Pro",
      "Max",
    ]);
    const dialogText = screen.getByRole("dialog").textContent ?? "";
    expect(dialogText.match(/Starter/g)).toHaveLength(2);
  });

  it("warns and enables confirm only once a different plan is chosen", () => {
    renderDialog();
    expect(
      screen.queryByText("admin.plans.changePlanDialog.warning"),
    ).toBeNull();
    expect(
      screen
        .getByText("admin.plans.changePlanDialog.confirm")
        .closest("button"),
    ).toBeDisabled();

    fireEvent.change(screen.getByTestId("select-mock"), {
      target: { value: "promax" },
    });

    expect(
      screen.getByText("admin.plans.changePlanDialog.warning"),
    ).toBeInTheDocument();
    expect(
      screen
        .getByText("admin.plans.changePlanDialog.confirm")
        .closest("button"),
    ).not.toBeDisabled();
  });

  it("sends only the new plan name, as the Quotas page did", async () => {
    const { onClose, onChanged } = renderDialog();
    fireEvent.change(screen.getByTestId("select-mock"), {
      target: { value: "promax" },
    });
    fireEvent.click(screen.getByText("admin.plans.changePlanDialog.confirm"));

    await waitFor(() =>
      expect(mockUpdateWorkspacePlan).toHaveBeenCalledTimes(1),
    );
    expect(mockUpdateWorkspacePlan).toHaveBeenCalledWith("ws-pro", {
      plan_name: "promax",
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        description:
          'admin.plans.messages.changeSuccess {"oldPlan":"PRO","newPlan":"PROMAX"}',
      }),
    );
  });

  it("treats a workspace without a plan as free", () => {
    renderDialog({ ...WORKSPACE, planName: "" });
    expect((screen.getByTestId("select-mock") as HTMLSelectElement).value).toBe(
      "free",
    );
  });

  it("toasts the server message and stays open when the change fails", async () => {
    mockUpdateWorkspacePlan.mockRejectedValue(new Error("workspace not found"));
    const { onClose, onChanged } = renderDialog();
    fireEvent.change(screen.getByTestId("select-mock"), {
      target: { value: "basic" },
    });
    fireEvent.click(screen.getByText("admin.plans.changePlanDialog.confirm"));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: "workspace not found",
          variant: "destructive",
        }),
      ),
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });
});
