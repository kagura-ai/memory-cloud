import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { AdminSections } from "./AdminSections";

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

vi.mock("@/lib/api/workspaces", () => ({
  getContextUserActivity: vi.fn().mockResolvedValue({ users: [] }),
  getWorkspaceMemberUsage: vi.fn().mockResolvedValue({ members: [] }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: () => "3h ago",
}));

describe("AdminSections", () => {
  it("renders nothing when user is not admin", () => {
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: { current_user_role: "member" },
    });

    const { container } = render(
      <AdminSections selectedContextId={null} currentWorkspaceId="ws-1" />,
    );

    expect(container.innerHTML).toBe("");
  });

  it("renders the activity heading for an admin", () => {
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: { current_user_role: "admin" },
    });

    render(
      <AdminSections selectedContextId={null} currentWorkspaceId="ws-1" />,
    );

    expect(screen.getByText("adminMemberActivity")).toBeInTheDocument();
  });

  it("shows activity immediately without an accordion", async () => {
    mockUseWorkspace.mockReturnValue({
      currentWorkspace: { current_user_role: "owner" },
    });
    render(<AdminSections selectedContextId="ctx-1" currentWorkspaceId="ws-1" />);
    expect(screen.getByRole("heading", { name: "adminMemberActivity" })).toBeVisible();
    expect(screen.getByText("userActivity")).toBeVisible();
    expect(screen.queryByRole("button", { name: "adminMemberActivity" })).not.toBeInTheDocument();
    await screen.findByText("noUserActivity");
  });
});
