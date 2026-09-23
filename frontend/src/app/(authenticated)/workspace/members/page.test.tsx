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
  act,
  render,
  screen,
  waitFor,
  cleanup,
  fireEvent,
} from "@testing-library/react";

import WorkspaceMembersPage from "./page";
import { ApiError } from "@/lib/api/base";
import { createInvitation } from "@/lib/api/invitations";
import { updateMemberRole } from "@/lib/api/workspaces";
import { normalizeGate } from "@/lib/gates/featureGates";

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

// Keys pass through as text. The #1644 refusal keys also echo their ICU
// params so the gate's values are assertable; every other key stays bare,
// because existing cases match exact text on keys that take params.
const ECHO_PARAMS = new Set(["invitePlanRequired", "memberSeatsFull"]);
const stableT = (k: string, params?: Record<string, unknown>) =>
  params && ECHO_PARAMS.has(k) ? `${k} ${JSON.stringify(params)}` : k;
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
  useLocale: () => "en",
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => mockUseAuth() }));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
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
  mockToast.mockReset();
  vi.mocked(createInvitation).mockReset();
  vi.mocked(updateMemberRole).mockReset();
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

/** An ApiError exactly as lib/api/base.ts would build it from this body. */
function refusal(
  status: number,
  error: string,
  message: string,
  details: Record<string, unknown> = {},
): ApiError {
  return new ApiError({
    error,
    message,
    status,
    details,
    gate: normalizeGate(status, error, details),
  });
}

/**
 * #1644 J-10 — the invitation list and the member quota are admin-only reads.
 * A role refusal (AUTH-101) is expected and swallowed; any other 403 is no
 * longer mistaken for one.
 */
describe("WorkspaceMembersPage admin-only reads swallow the role gate only (#1644)", () => {
  function consoleErrorsMatching(
    spy: ReturnType<typeof vi.spyOn>,
    text: string,
  ) {
    return spy.mock.calls.filter((call: unknown[]) => call[0] === text);
  }

  it("still swallows an AUTH-101 refusal on both reads", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    setupWithRole("admin");
    const role = refusal(403, "AUTH-101", "Insufficient permissions");
    mockListInvitations.mockRejectedValue(role);
    mockGetMemberQuota.mockRejectedValue(role);

    render(<WorkspaceMembersPage />);
    await waitFor(() => expect(mockGetMemberQuota).toHaveBeenCalled());
    await waitFor(() => expect(mockListInvitations).toHaveBeenCalled());

    expect(consoleErrorsMatching(spy, "Failed to load invitations:")).toEqual(
      [],
    );
    expect(consoleErrorsMatching(spy, "Failed to load member quota:")).toEqual(
      [],
    );
    spy.mockRestore();
  });

  it("no longer swallows a 403 that is not a role refusal", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    setupWithRole("admin");
    const bare = refusal(403, "HTTP-403", "Forbidden");
    mockListInvitations.mockRejectedValue(bare);
    mockGetMemberQuota.mockRejectedValue(bare);

    render(<WorkspaceMembersPage />);

    await waitFor(() =>
      expect(
        consoleErrorsMatching(spy, "Failed to load invitations:"),
      ).toHaveLength(1),
    );
    await waitFor(() =>
      expect(
        consoleErrorsMatching(spy, "Failed to load member quota:"),
      ).toHaveLength(1),
    );
    spy.mockRestore();
  });
});

/** #1644 C6 — the invite refusal is read from err.gate, not server English. */
describe("WorkspaceMembersPage invite refusal reads err.gate (#1644)", () => {
  // The page logs every refusal it catches; keep the run output readable.
  let quiet: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => quiet.mockRestore());

  async function submitAdminInvite() {
    setupWithRole("owner", "pro");
    render(<WorkspaceMembersPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /inviteMember/ }),
    );
    fireEvent.change(await screen.findByPlaceholderText("emailPlaceholder"), {
      target: { value: "new@example.com" },
    });
    // An admin invite needs no context selection.
    fireEvent.change(screen.getByDisplayValue("roleMemberDesc"), {
      target: { value: "admin" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "createInvitation" }));
    });
  }

  it("renders the plan refusal in the reader's language with the required tier", async () => {
    vi.mocked(createInvitation).mockRejectedValue(
      refusal(403, "FEAT-001", "Feature 'team_invitations' not available.", {
        gate: "plan",
        feature: "team_invitations",
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "basic",
      }),
    );
    await submitAdminInvite();

    expect(
      await screen.findByText('invitePlanRequired {"plan":"L"}'),
    ).toBeInTheDocument();
  });

  it("renders the seat-cap message from err.gate instead of the server's English", async () => {
    const serverText =
      "Member limit reached (5 seats). Current members: 4, Pending invitations: 1.";
    vi.mocked(createInvitation).mockRejectedValue(
      refusal(429, "QUOTA-001", serverText, {
        gate: "quota",
        quota_type: "members",
        current: 5,
        limit: 5,
        required_plan: null,
        required_plan_display: null,
        current_plan: "pro",
      }),
    );
    await submitAdminInvite();

    expect(
      await screen.findByText('memberSeatsFull {"current":5,"limit":5}'),
    ).toBeInTheDocument();
    expect(screen.queryByText(serverText)).toBeNull();
  });

  it("keeps the verbatim fallback for a refusal with no gate", async () => {
    vi.mocked(createInvitation).mockRejectedValue(
      refusal(409, "HTTP-409", "An invitation for this email already exists", {
        detail: "An invitation for this email already exists",
      }),
    );
    await submitAdminInvite();

    expect(
      await screen.findByText("An invitation for this email already exists"),
    ).toBeInTheDocument();
  });
});

/**
 * #1644 J-11 — the role-change 403s are raw HTTPExceptions with no semantic
 * code, so their prose is still matched; a genuine AUTH-101 no longer is.
 */
describe("WorkspaceMembersPage role-change refusal (#1644)", () => {
  // The page logs every refusal it catches; keep the run output readable.
  let quiet: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => quiet.mockRestore());

  async function changeBobToAdmin() {
    setupWithRole("owner", "pro");
    mockListMembers.mockResolvedValue([
      {
        user_id: "user-2",
        user_name: "Bob",
        user_email: "bob@example.com",
        role: "member",
        joined_at: null,
        allowed_context_ids: null,
      },
    ]);
    render(<WorkspaceMembersPage />);
    const select = await screen.findByDisplayValue("member");
    await act(async () => {
      fireEvent.change(select, { target: { value: "admin" } });
    });
    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
    return mockToast.mock.calls[0][0] as { description: string };
  }

  it("still maps the raw 'own role' 403 to its localized copy", async () => {
    vi.mocked(updateMemberRole).mockRejectedValue(
      refusal(403, "HTTP-403", "Cannot modify your own role"),
    );

    expect((await changeBobToAdmin()).description).toBe(
      "cannotModifyOwnRoleDesc",
    );
  });

  it("still maps the raw 'owner can change' 403 to its localized copy", async () => {
    vi.mocked(updateMemberRole).mockRejectedValue(
      refusal(403, "HTTP-403", "Only the owner can change the owner's role"),
    );

    expect((await changeBobToAdmin()).description).toBe(
      "onlyOwnerCanChangeOwner",
    );
  });

  it("does not run an AUTH-101 role refusal through the prose matcher", async () => {
    // A role refusal whose server text happens to contain the matched words.
    vi.mocked(updateMemberRole).mockRejectedValue(
      refusal(403, "AUTH-101", "Only the owner can change workspace roles"),
    );

    expect((await changeBobToAdmin()).description).toBe(
      "Only the owner can change workspace roles",
    );
  });
});
