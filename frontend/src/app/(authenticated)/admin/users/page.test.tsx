/**
 * Admin Users page categories (#2765): Users / Plan tiers / Plan changes,
 * synced to `?tab=` with useTabParam; an unknown value falls back to Users.
 * The tier and audit tables have their own tests under
 * components/admin/plans; here they are stubs.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import en from "@/messages/en.json";
import ja from "@/messages/ja.json";

const mockGet = vi.fn();
const mockPut = vi.fn();
vi.mock("@/lib/api", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    put: (...args: unknown[]) => mockPut(...args),
  },
}));

let mockQuery = "";
const mockReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: mockReplace }),
  useSearchParams: () => new URLSearchParams(mockQuery),
  usePathname: () => "/admin/users",
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock("@/components/common/PageContainer", () => ({
  PageContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));
vi.mock("@/components/admin/plans/PlanTiersTable", () => ({
  PlanTiersTable: () => <div data-testid="plan-tiers-table" />,
}));
vi.mock("@/components/admin/plans/PlanAuditTable", () => ({
  PlanAuditTable: () => <div data-testid="plan-audit-table" />,
}));

import AdminUsersPage from "./page";

const USER = {
  id: "u1",
  email: "someone@example.com",
  name: "Someone",
  role: "user",
  created_at: "2026-01-01T00:00:00Z",
  memory_count: 3,
  is_active: true,
  workspaces: [],
};

beforeEach(() => {
  mockQuery = "";
  mockReplace.mockReset();
  mockGet.mockReset();
  mockGet.mockResolvedValue({ users: [USER], total: 1 });
  mockPut.mockReset();
  mockPut.mockResolvedValue({});
});

describe("AdminUsersPage categories (#2765)", () => {
  it("shows the three categories and the Users list by default", async () => {
    render(<AdminUsersPage />);

    expect(await screen.findByText("Someone")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "tabs.users" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      screen.getByRole("tab", { name: "tabs.planTiers" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "tabs.planChanges" }),
    ).toBeInTheDocument();
    expect(screen.getByText("tabHelp.users")).toBeInTheDocument();
    expect(screen.queryByTestId("plan-tiers-table")).toBeNull();
    // The canonical URL form is promoted on first mount.
    expect(mockReplace).toHaveBeenCalledWith("/admin/users?tab=users");
  });

  it("deep-links to Plan tiers", async () => {
    mockQuery = "tab=plan-tiers";
    render(<AdminUsersPage />);

    expect(await screen.findByTestId("plan-tiers-table")).toBeInTheDocument();
    expect(screen.getByText("tabHelp.planTiers")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "tabs.planTiers" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByText("Someone")).toBeNull();
  });

  it("deep-links to Plan changes", async () => {
    mockQuery = "tab=plan-changes";
    render(<AdminUsersPage />);

    expect(await screen.findByTestId("plan-audit-table")).toBeInTheDocument();
    expect(screen.getByText("tabHelp.planChanges")).toBeInTheDocument();
  });

  it("falls back to Users for an unknown ?tab= value", async () => {
    mockQuery = "tab=workspaces";
    render(<AdminUsersPage />);

    expect(await screen.findByText("Someone")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "tabs.users" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("writes the chosen category to the URL", async () => {
    render(<AdminUsersPage />);
    await screen.findByText("Someone");
    mockReplace.mockReset();

    fireEvent.mouseDown(screen.getByRole("tab", { name: "tabs.planChanges" }));

    expect(mockReplace).toHaveBeenCalledWith("/admin/users?tab=plan-changes");
  });
});

describe("AdminUsersPage system role change (#2854)", () => {
  const ADMIN = { ...USER, id: "u2", name: "Admin Two", role: "admin" };
  const OTHER_ADMIN = { ...USER, id: "u3", name: "Admin Three", role: "admin" };

  const openRowMenu = (index: number) => {
    const trigger = screen.getAllByRole("button", { name: "Row actions" })[
      index
    ];
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  };

  it("offers only admin and user: a user is promoted to admin", async () => {
    render(<AdminUsersPage />);
    await screen.findByText("Someone");

    openRowMenu(0);
    fireEvent.click(await screen.findByText("actions.promoteToAdmin"));

    expect(mockPut).toHaveBeenCalledTimes(1);
    expect(mockPut).toHaveBeenCalledWith("/api/v1/admin/users/u1/role", {
      role: "admin",
    });
  });

  it("offers only admin and user: an admin is demoted to user", async () => {
    mockGet.mockResolvedValue({ users: [ADMIN, OTHER_ADMIN], total: 2 });
    render(<AdminUsersPage />);
    await screen.findByText("Admin Two");

    openRowMenu(0);
    fireEvent.click(await screen.findByText("actions.demoteFromAdmin"));

    expect(mockPut).toHaveBeenCalledTimes(1);
    expect(mockPut).toHaveBeenCalledWith("/api/v1/admin/users/u2/role", {
      role: "user",
    });
  });

  it("offers no other role in the row menu", async () => {
    render(<AdminUsersPage />);
    await screen.findByText("Someone");

    openRowMenu(0);
    await screen.findByText("actions.promoteToAdmin");

    expect(screen.queryByText(/read.?only/i)).toBeNull();
  });

  it("takes the role labels from the messages", async () => {
    mockGet.mockResolvedValue({ users: [USER, ADMIN], total: 2 });
    render(<AdminUsersPage />);
    await screen.findByText("Admin Two");

    expect(screen.getByText("table.systemAdmin")).toBeTruthy();
    expect(screen.getByText("table.userRole")).toBeTruthy();
    for (const messages of [en, ja]) {
      const { actions, table } = messages.admin.users;
      for (const label of [
        actions.promoteToAdmin,
        actions.demoteFromAdmin,
        table.systemAdmin,
        table.userRole,
      ]) {
        expect(label).toBeTruthy();
      }
    }
    expect(en.admin.users.actions.promoteToAdmin).toBe("Promote to System Admin");
    expect(en.admin.users.actions.demoteFromAdmin).toBe("Demote from System Admin");
  });
});
