/**
 * Tests for the workspace members page redirect guard (Issue #398).
 *
 * Workspace member/viewer roles must NOT see the members roster — the page
 * pushes them back to /workspace/dashboard. Admin/owner stay. The redirect
 * is suppressed while WorkspaceContext is still loading so admins don't
 * flash through the dashboard during hydration.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, waitFor, cleanup } from "@testing-library/react";

import WorkspaceMembersPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockListMembers = vi.fn();
const mockListInvitations = vi.fn();
const mockGetMemberQuota = vi.fn();
const mockGetContexts = vi.fn();

vi.mock("@/lib/api/workspaces", () => ({
  listMembers: (...a: unknown[]) => mockListMembers(...a),
  addMember: vi.fn(),
  updateMemberRole: vi.fn(),
  removeMember: vi.fn(),
  updateMemberContextAccess: vi.fn(),
}));
vi.mock("@/lib/api/invitations", () => ({
  listInvitations: (...a: unknown[]) => mockListInvitations(...a),
  createInvitation: vi.fn(),
  deleteInvitation: vi.fn(),
  getMemberQuota: (...a: unknown[]) => mockGetMemberQuota(...a),
}));
vi.mock("@/lib/api/contexts", () => ({
  getContexts: (...a: unknown[]) => mockGetContexts(...a),
}));

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: vi.fn() }),
}));

const stableT = (k: string) => k;
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => mockUseAuth() }));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

// ---------- Helpers ----------------------------------------------------------

type Role = "owner" | "admin" | "member" | "viewer";

const WORKSPACE_ID = "ws-1";

function setupWithRole(role: Role) {
  mockUseAuth.mockReturnValue({ user: { id: "user-1" } });
  mockUseWorkspace.mockReturnValue({
    currentWorkspaceId: WORKSPACE_ID,
    currentWorkspace: {
      id: WORKSPACE_ID,
      plan_name: "pro",
      current_user_role: role,
    },
    loading: false,
  });
  mockListMembers.mockResolvedValue([]);
  mockListInvitations.mockResolvedValue([]);
  mockGetMemberQuota.mockResolvedValue({
    plan_name: "pro",
    members_used: 0,
    members_limit: 100,
    upgrade_required: false,
  });
  mockGetContexts.mockResolvedValue({ contexts: [] });
}

beforeEach(() => {
  mockUseAuth.mockReset();
  mockUseWorkspace.mockReset();
  mockListMembers.mockReset();
  mockListInvitations.mockReset();
  mockGetMemberQuota.mockReset();
  mockGetContexts.mockReset();
  mockPush.mockReset();
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("WorkspaceMembersPage redirect guard (#398)", () => {
  it.each(["member", "viewer"] as const)(
    "redirects %s to /workspace/dashboard",
    async (role) => {
      setupWithRole(role);
      render(<WorkspaceMembersPage />);
      await waitFor(() =>
        expect(mockPush).toHaveBeenCalledWith("/workspace/dashboard"),
      );
    },
  );

  it.each(["admin", "owner"] as const)("does NOT redirect %s", async (role) => {
    setupWithRole(role);
    render(<WorkspaceMembersPage />);
    // Wait for the data-load useEffect to fire so we know the redirect
    // useEffect has had a chance too. listMembers being called is a
    // good proxy for "page mounted past the role check".
    await waitFor(() => expect(mockListMembers).toHaveBeenCalled());
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("does NOT redirect while WorkspaceContext is still loading", async () => {
    mockUseAuth.mockReturnValue({ user: { id: "user-1" } });
    mockUseWorkspace.mockReturnValue({
      currentWorkspaceId: null,
      currentWorkspace: null,
      loading: true,
    });
    mockListMembers.mockResolvedValue([]);
    mockListInvitations.mockResolvedValue([]);
    mockGetMemberQuota.mockResolvedValue({
      current_members: 0,
      pending_invitations: 0,
      total_used: 0,
      limit: 100,
      available: 100,
      percentage: 0,
      can_invite: true,
    });
    mockGetContexts.mockResolvedValue({ contexts: [] });

    render(<WorkspaceMembersPage />);
    // Give React a chance to flush effects. No redirect should fire because
    // workspaceLoading is true.
    await new Promise((r) => setTimeout(r, 50));
    expect(mockPush).not.toHaveBeenCalled();
  });
});
