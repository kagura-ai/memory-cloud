/**
 * Tests for the workspace members page redirect guard (Issue #398).
 *
 * Workspace member/viewer roles must NOT see the members roster — the page
 * pushes them back to /workspace/dashboard. Admin/owner stay. The redirect
 * is suppressed while WorkspaceContext is still loading so admins don't
 * flash through the dashboard during hydration.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";

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

// #1643: useCanUpgrade reads /system/info. Without this mock the real hook
// fires a jsdom fetch, retries three times and leaves a module-level cache
// that leaks between cases in this file. `null` = still resolving.
let mockFeatures: Record<string, boolean> | null = { plan_page: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

// ---------- Helpers ----------------------------------------------------------

type Role = "owner" | "admin" | "member" | "viewer";

const WORKSPACE_ID = "ws-1";

function setupWithRole(role: Role, plan: string = "pro") {
  mockUseAuth.mockReturnValue({ user: { id: "user-1" } });
  mockUseWorkspace.mockReturnValue({
    currentWorkspaceId: WORKSPACE_ID,
    currentWorkspace: {
      id: WORKSPACE_ID,
      plan_name: plan,
      current_user_role: role,
    },
    loading: false,
  });
  mockListMembers.mockResolvedValue([]);
  mockListInvitations.mockResolvedValue([]);
  mockGetMemberQuota.mockResolvedValue({
    plan_name: plan,
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
  mockFeatures = { plan_page: true };
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
    // Anchor on a positive signal (PageHeader renders) so the page's first
    // effect cycle is known to have run. The negative assertion that
    // follows would be vacuously true right after render() — without an
    // anchor a regression that fires the redirect on the next tick could
    // still pass. Both API fetches are also gated on workspaceLoading so
    // there's no API call to anchor on during the loading state.
    await waitFor(() => {
      expect(screen.getByText("membersTitle")).toBeInTheDocument();
    });
    expect(mockPush).not.toHaveBeenCalled();
  });
});

// Team invitations are Pro-or-better (#1548): promax must get the same
// treatment as pro, and free/basic still route to the plan page.
describe("WorkspaceMembersPage invite gate", () => {
  it.each(["pro", "promax"] as const)(
    "%s: invite is enabled and does not redirect to the plan page",
    async (plan) => {
      setupWithRole("owner", plan);
      render(<WorkspaceMembersPage />);
      const invite = await screen.findByRole("button", {
        name: /inviteMember/,
      });
      expect(invite).not.toBeDisabled();
      expect(invite).not.toHaveTextContent("proPlanRequired");
      fireEvent.click(invite);
      expect(mockPush).not.toHaveBeenCalledWith("/workspace/settings/plan");
    },
  );

  it.each(["free", "basic"] as const)(
    "%s: invite is disabled with the pro-required hint",
    async (plan) => {
      setupWithRole("owner", plan);
      render(<WorkspaceMembersPage />);
      const invite = await screen.findByRole("button", {
        name: /inviteMember/,
      });
      expect(invite).toBeDisabled();
      expect(invite).toHaveTextContent("proPlanRequired");
    },
  );
});

/**
 * #1643 — the seat-limit surfaces inside the invite dialog.
 *
 * Both used to render a <Link> wrapping a <button> (an <a> containing a
 * <button>, invalid interactive nesting). They are now a single <Button
 * asChild><Link>, and they only render where the Plan page is reachable; the
 * seat-limit copy stays either way.
 */
describe("WorkspaceMembersPage seat-limit upgrade links (#1643)", () => {
  const AT_LIMIT_QUOTA = {
    current_members: 5,
    pending_invitations: 0,
    total_used: 5,
    limit: 5,
    available: 0,
    percentage: 100,
    can_invite: false,
  };

  async function openInviteDialogAtLimit(role: Role = "owner") {
    setupWithRole(role, "pro");
    mockGetMemberQuota.mockResolvedValue(AT_LIMIT_QUOTA);

    render(<WorkspaceMembersPage />);

    const invite = await screen.findByRole("button", { name: /inviteMember/ });
    fireEvent.click(invite);
    // The seat-limit copy is the explanation — it renders in every case here.
    await waitFor(() =>
      expect(screen.getAllByText("seatLimitReached").length).toBeGreaterThan(0),
    );
  }

  it("seat limit reached, plan_page off: the limit copy renders with no upgrade link", async () => {
    mockFeatures = {};
    await openInviteDialogAtLimit();

    expect(screen.getByText("seatLimitReachedDesc")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "upgradeToAddMembers" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "upgradeToAddMembers" }),
    ).toBeNull();
  });

  it("seat limit reached, admin: the limit copy renders with no upgrade link", async () => {
    await openInviteDialogAtLimit("admin");

    expect(screen.getByText("seatLimitReachedDesc")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "upgradeToAddMembers" }),
    ).toBeNull();
  });

  it("seat limit reached, /system/info pending: no upgrade link", async () => {
    mockFeatures = null;
    await openInviteDialogAtLimit();

    expect(
      screen.queryByRole("link", { name: "upgradeToAddMembers" }),
    ).toBeNull();
  });

  it("seat limit reached, owner on a plan_page deployment: anchors, not nested buttons", async () => {
    await openInviteDialogAtLimit();

    // One for the seat badge, one for the at-limit panel — both plain anchors.
    const links = screen.getAllByRole("link", { name: "upgradeToAddMembers" });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link).toHaveAttribute("href", "/workspace/settings/plan");
      // The invalid <a><button> nesting is gone: no button inside the anchor,
      // and no standalone button carrying the same label.
      expect(link.querySelector("button")).toBeNull();
    }
    expect(
      screen.queryByRole("button", { name: "upgradeToAddMembers" }),
    ).toBeNull();
  });
});
