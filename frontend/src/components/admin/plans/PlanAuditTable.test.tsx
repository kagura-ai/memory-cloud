/**
 * PlanAuditTable (moved out of the Quotas page into the Users page's Plan
 * changes tab in #2765). The "changed by" actor renders as name + email,
 * linked to the admin user detail; `changed_by` carries the raw user id (link
 * target). An actor that no longer resolves renders its raw id unlinked.
 */

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PlanAuditTable } from "./PlanAuditTable";

const mockGetAdminPlanAudit = vi.fn();
vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    getAdminPlanAudit: (...args: unknown[]) => mockGetAdminPlanAudit(...args),
  };
});

vi.mock("next-intl", () => ({
  useTranslations: (namespace: string) => (key: string) =>
    namespace ? `${namespace}.${key}` : key,
  useLocale: () => "en",
}));

const auditEntry = (over: Record<string, unknown>) => ({
  id: 1,
  workspace_id: "ws-1",
  workspace_name: "Personal Workspace",
  old_plan: "free",
  new_plan: "pro",
  changed_by: "100000000000000000001",
  changed_by_name: "Example Admin",
  changed_by_email: "admin@example.com",
  changed_at: "2026-06-02T11:52:41Z",
  reason: null,
  ...over,
});

beforeEach(() => {
  mockGetAdminPlanAudit.mockReset();
});

describe("PlanAuditTable", () => {
  it("requests the last 100 changes, as the Quotas page did", async () => {
    mockGetAdminPlanAudit.mockResolvedValue([auditEntry({})]);
    render(<PlanAuditTable />);
    await screen.findByText("Personal Workspace");
    expect(mockGetAdminPlanAudit).toHaveBeenCalledWith(100);
  });

  it("renders name + email and links them to /admin/users/{changed_by}", async () => {
    mockGetAdminPlanAudit.mockResolvedValue([auditEntry({})]);
    render(<PlanAuditTable />);

    const name = await screen.findByText("Example Admin");
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    const link = name.closest("a");
    expect(link).toHaveAttribute("href", "/admin/users/100000000000000000001");
    expect(link).toHaveTextContent("admin@example.com");
  });

  it("falls back to email only when the actor has no display name", async () => {
    mockGetAdminPlanAudit.mockResolvedValue([
      auditEntry({ changed_by_name: null }),
    ]);
    render(<PlanAuditTable />);

    const link = await screen.findByRole("link", { name: "admin@example.com" });
    expect(link).toHaveAttribute("href", "/admin/users/100000000000000000001");
    expect(screen.queryByText("Example Admin")).toBeNull();
  });

  it("shows the raw id without a link when the actor no longer resolves", async () => {
    mockGetAdminPlanAudit.mockResolvedValue([
      auditEntry({
        changed_by: "erased-pseudonym",
        changed_by_name: null,
        changed_by_email: null,
      }),
    ]);
    render(<PlanAuditTable />);

    expect(await screen.findByText("erased-pseudonym")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "erased-pseudonym" })).toBeNull();
  });

  it("renders the empty state when nothing changed", async () => {
    mockGetAdminPlanAudit.mockResolvedValue([]);
    render(<PlanAuditTable />);
    expect(
      await screen.findByText("admin.common.noChanges"),
    ).toBeInTheDocument();
  });

  it("renders the error banner when the audit fails to load", async () => {
    mockGetAdminPlanAudit.mockRejectedValue(new Error("boom"));
    render(<PlanAuditTable />);
    expect(
      await screen.findByText("admin.plans.messages.loadError"),
    ).toBeInTheDocument();
    expect(screen.queryByText("admin.common.noChanges")).toBeNull();
  });
});
