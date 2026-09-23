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
import type { PlanTierFeature } from "@/lib/api/workspaces";
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

// Keys pass through as text. The gate descriptions also echo their ICU
// params so the gate's values are assertable; every other key stays bare,
// because existing cases match exact text on keys that take params.
const ECHO_PARAMS = new Set([
  "plan.description",
  "quota.description",
  "quota.upsell",
]);
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

// #1645: the invite gate reads the shared tier matrix (`null` = still
// resolving). Default: the OSS matrix, so `plan_name` decides exactly as the
// tier's row does.
// `max_members` is the seat cap's column (#1646 Q3): the first tier above
// the workspace's limit is the one the seat notice names.
const OSS_TIERS = [
  { name: "free", display_name: "S", team_invitations: false, max_members: 1 },
  { name: "basic", display_name: "M", team_invitations: false, max_members: 3 },
  { name: "pro", display_name: "L", team_invitations: true, max_members: 5 },
  { name: "promax", display_name: "XL", team_invitations: true, max_members: 20 },
] as unknown as PlanTierFeature[];
let mockTiers: PlanTierFeature[] | null = OSS_TIERS;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => mockTiers,
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
  mockTiers = OSS_TIERS;
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
      // #1646: an allowed gate renders no notice, so nothing describes it.
      expect(invite).not.toHaveAttribute("aria-describedby");
      expect(screen.queryByText("plan.hint")).toBeNull();
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
      // #1646 P9: the gate's hint sits beside the button, which points at it.
      expect(invite).toHaveAccessibleDescription("plan.hint");
    },
  );
  it("invite stays disabled with no plan suffix while the matrix resolves (#1645)", async () => {
    // Before, the ordinal pro-or-better check was false for an unknown plan,
    // so every tenant — entitled ones included — saw "(Pro Plan)" until the
    // plan was known.
    mockTiers = null;
    setupWithRole("owner", "pro");
    render(<WorkspaceMembersPage />);
    const invite = await screen.findByRole("button", {
      name: /inviteMember/,
    });
    expect(invite).toBeDisabled();
    // A pending gate renders no notice at all (#1646 hard rule 1).
    expect(invite).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText("plan.hint")).toBeNull();
    expect(screen.queryByText("role.admin.hint")).toBeNull();
  });

  it("team_invitations from the matrix, not the tier rank (#1645)", async () => {
    // An operator gives basic team invitations and takes them from promax.
    mockTiers = OSS_TIERS.map((t) => ({
      ...t,
      team_invitations: t.name === "basic",
    }));
    setupWithRole("owner", "basic");
    const { unmount } = render(<WorkspaceMembersPage />);
    expect(
      await screen.findByRole("button", { name: /inviteMember/ }),
    ).not.toBeDisabled();
    unmount();

    setupWithRole("owner", "promax");
    render(<WorkspaceMembersPage />);
    const invite = await screen.findByRole("button", {
      name: /inviteMember/,
    });
    expect(invite).toBeDisabled();
    expect(invite).toHaveAccessibleDescription("plan.hint");
  });

  it("an admin passes the admin-minimum role half, so the plan half speaks (#1645)", async () => {
    // team_invitations is admin-minimum in GATE_SPECS: an admin on a tier
    // without invitations is told about the plan, not about the role.
    // (Members and viewers never reach this control: the page redirects
    // them, #398; role-before-plan itself is pinned in featureGates.test.)
    setupWithRole("admin", "free");
    render(<WorkspaceMembersPage />);
    const invite = await screen.findByRole("button", {
      name: /inviteMember/,
    });
    expect(invite).toBeDisabled();
    expect(invite).toHaveAccessibleDescription("plan.hint");
    expect(screen.queryByText("role.admin.hint")).toBeNull();
  });
});

// #1646 P9/R1: the invite control renders the gate descriptor through the
// `control` notice — badge and hint beside a disabled button that points at
// the hint, a CTA only where the member may upgrade, and no tier name baked
// into the button label.
describe("WorkspaceMembersPage invite control notice (#1646 P9/R1)", () => {
  async function findInvite() {
    return screen.findByRole("button", { name: /inviteMember/ });
  }

  it("plan gate, owner on a Plan-page deployment: badge, hint and a CTA to the Plan page", async () => {
    setupWithRole("owner", "free");
    render(<WorkspaceMembersPage />);
    const invite = await findInvite();

    expect(invite).toBeDisabled();
    // The label is the action alone — the old "(Pro Plan)" suffix is gone.
    expect(invite).toHaveTextContent(/^inviteMember$/);
    expect(invite).toHaveAccessibleDescription("plan.hint");
    expect(screen.getByText("plan.badge")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "plan.action" }));
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
  });

  it("plan gate, admin: the hint stays and the CTA is withheld", async () => {
    setupWithRole("admin", "free");
    render(<WorkspaceMembersPage />);
    const invite = await findInvite();

    expect(invite).toHaveAccessibleDescription("plan.hint");
    expect(screen.queryByRole("button", { name: "plan.action" })).toBeNull();
  });

  it("plan gate, owner with the Plan page off: the hint stays and the CTA is withheld", async () => {
    mockFeatures = {};
    setupWithRole("owner", "free");
    render(<WorkspaceMembersPage />);
    const invite = await findInvite();

    expect(invite).toHaveAccessibleDescription("plan.hint");
    expect(screen.queryByRole("button", { name: "plan.action" })).toBeNull();
  });

  it("role gate (R1): the role hint once, no badge, no CTA", async () => {
    // Members and viewers are redirected before the roster loads (#398), so
    // the role state reaches the control only when the role drops under a
    // loaded page. It must still read right: role outranks plan, and the
    // role badge would repeat the hint word for word.
    setupWithRole("admin", "pro");
    const { rerender } = render(<WorkspaceMembersPage />);
    expect(await findInvite()).not.toBeDisabled();

    mockUseWorkspace.mockReturnValue({
      currentWorkspaceId: WORKSPACE_ID,
      currentWorkspace: {
        id: WORKSPACE_ID,
        plan_name: "free",
        current_user_role: "member",
      },
      loading: false,
    });
    rerender(<WorkspaceMembersPage />);

    const invite = await findInvite();
    expect(invite).toBeDisabled();
    expect(invite).toHaveAccessibleDescription("role.admin.hint");
    expect(screen.getAllByText("role.admin.hint")).toHaveLength(1);
    expect(screen.queryByText("role.admin.badge")).toBeNull();
    expect(screen.queryByText("plan.hint")).toBeNull();
    expect(screen.queryByRole("button", { name: "plan.action" })).toBeNull();
  });
});

// #1645: the invite control over every cell of {tier matrix} x
// {/system/info} x {role, tier}. A failed matrix reads as `null`, exactly like
// a pending one (the hook suite pins that). The invite gate has no deployment
// flag, so /system/info must change nothing here. Members and viewers never
// reach the control (the page redirects them, #398); their role-before-plan
// cells are pinned on the hook in useFeatureGate.test.tsx.
describe("WorkspaceMembersPage invite gate — the whole truth table (#1645)", () => {
  const MATRIX: Record<string, PlanTierFeature[] | null> = {
    "pending-or-failed": null,
    resolved: OSS_TIERS,
  };
  const INFO: Record<string, Record<string, boolean> | null> = {
    pending: null,
    "plan_page on": { plan_page: true },
    "plan_page off": { plan_page: false },
    "failed ({})": {},
  };
  const CELLS = Object.keys(MATRIX).flatMap((m) =>
    Object.keys(INFO).flatMap((i) =>
      (["admin", "owner"] as const).flatMap((role) =>
        (["free", "pro"] as const).map((plan) => [m, i, role, plan] as const),
      ),
    ),
  );

  it.each(CELLS)(
    "matrix %s, /system/info %s, %s on %s",
    async (m, i, role, plan) => {
      mockTiers = MATRIX[m];
      mockFeatures = INFO[i];
      setupWithRole(role, plan);
      render(<WorkspaceMembersPage />);
      const invite = await screen.findByRole("button", {
        name: /inviteMember/,
      });

      const known = m === "resolved";
      // Usable only once the matrix says this tier invites.
      if (known && plan === "pro") {
        expect(invite).not.toBeDisabled();
      } else {
        expect(invite).toBeDisabled();
      }
      // An admin passes the role half; the plan half speaks only once the
      // matrix has answered — never a plan hint while it is pending.
      expect(screen.queryByText("role.admin.hint")).toBeNull();
      expect(screen.queryByText("plan.hint") !== null).toBe(
        known && plan === "free",
      );
      expect(invite).toHaveAccessibleDescription(
        known && plan === "free" ? "plan.hint" : "",
      );
    },
  );
});

/**
 * #1643 — the seat-limit surfaces inside the invite dialog.
 *
 * Both used to render a <Link> wrapping a <button> (an <a> containing a
 * <button>, invalid interactive nesting), and only rendered where the Plan
 * page is reachable; the seat-limit copy stayed either way. #1646 Q3 replaces
 * both with the gate notice: one CTA, a real button, under the same rule.
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
      expect(screen.getByText("quota.title")).toBeInTheDocument(),
    );
  }

  it("seat limit reached, plan_page off: the limit copy renders with no upgrade link", async () => {
    mockFeatures = {};
    await openInviteDialogAtLimit();

    expect(screen.getByText(/^quota\.description /)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "quota.action" })).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("seat limit reached, admin: the limit copy renders with no upgrade link", async () => {
    await openInviteDialogAtLimit("admin");

    expect(screen.getByText(/^quota\.description /)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "quota.action" })).toBeNull();
  });

  it("seat limit reached, /system/info pending: no upgrade link", async () => {
    mockFeatures = null;
    await openInviteDialogAtLimit();

    expect(screen.queryByRole("button", { name: "quota.action" })).toBeNull();
  });

  it("seat limit reached, owner on a plan_page deployment: one CTA to the Plan page, no nested anchor", async () => {
    await openInviteDialogAtLimit();

    // The two old <Link>s (seat badge + at-limit panel) are one notice CTA.
    const ctas = screen.getAllByRole("button", { name: "quota.action" });
    expect(ctas).toHaveLength(1);
    expect(screen.queryByRole("link")).toBeNull();
    fireEvent.click(ctas[0]);
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
  });
});

/**
 * #1646 Q3/Q4 — the seat cap is a gate notice; below it the seat count is
 * one plain line.
 */
describe("WorkspaceMembersPage seat cap notice (#1646 Q3/Q4)", () => {
  function quota(used: number, limit: number) {
    return {
      current_members: used,
      pending_invitations: 0,
      total_used: used,
      limit,
      available: Math.max(0, limit - used),
      percentage: limit > 0 ? (used / limit) * 100 : 0,
      can_invite: used < limit,
    };
  }

  async function openDialog(q: ReturnType<typeof quota>, role: Role = "owner") {
    setupWithRole(role, "pro");
    mockGetMemberQuota.mockResolvedValue(q);
    render(<WorkspaceMembersPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: /inviteMember/ }),
    );
    await screen.findByText("inviteTeamMember");
  }

  it.each([
    ["well below the cap", 1],
    ["at 80% (the old warning band)", 4],
  ] as const)(
    "%s: one muted seat line, no emoji, no notice, and the form",
    async (_label, used) => {
      await openDialog(quota(used, 5));

      const line = await screen.findByText(/^seatUsage/);
      expect(line).toHaveTextContent("seatUsage · seatsAvailable");
      expect(line).toHaveClass("text-muted-foreground");
      expect(line.textContent).not.toMatch(/[❌⚠ℹ]/u);
      expect(screen.queryByText("quota.title")).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.getByPlaceholderText("emailPlaceholder")).toBeInTheDocument();
    },
  );

  it("at the cap: the notice replaces the seat line and the form", async () => {
    await openDialog(quota(5, 5));

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("quota.title");
    expect(screen.queryByText(/^seatUsage/)).toBeNull();
    expect(screen.queryByPlaceholderText("emailPlaceholder")).toBeNull();
  });

  it("at the cap: the counts and the tier labels come from the descriptor, never the raw plan_name", async () => {
    await openDialog(quota(5, 5));

    const notice = await screen.findByRole("alert");
    // Vocabulary 3 is gone: no `plan_name.toUpperCase() || "PRO"`.
    expect(notice.textContent).not.toMatch(/PRO/);
    expect(notice).toHaveTextContent('"currentPlan":"L"');
    expect(notice).toHaveTextContent('"current":5,"limit":5');
    // The tier that raises the cap, from the matrix's `max_members`.
    expect(notice).toHaveTextContent('quota.upsell {"plan":"XL"');
    expect(notice).toHaveTextContent('"feature":"features.members.singular"');
  });

  it("at the cap with no served tier above it: no upsell sentence, no CTA", async () => {
    mockTiers = OSS_TIERS.map((t) => ({ ...t, max_members: 5 }));
    await openDialog(quota(5, 5));

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("quota.title");
    expect(notice.textContent).not.toContain("quota.upsell");
    expect(screen.queryByRole("button", { name: "quota.action" })).toBeNull();
  });

  it("an unknown cap (limit 0) never blocks: the form stays", async () => {
    await openDialog(quota(0, 0));

    expect(screen.queryByText("quota.title")).toBeNull();
    expect(screen.getByPlaceholderText("emailPlaceholder")).toBeInTheDocument();
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

/**
 * #1644 C6 — the invite refusal is read from err.gate, not server English.
 * #1646: rendered by the gate notice, not by the interim per-page keys.
 */
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

    // #1646: the gate notice, with the refusal's tier.
    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("plan.title");
    expect(notice).toHaveTextContent(/plan\.description \{"plan":"L"/);
    // The dialog's refusal carries no CTA of its own.
    expect(screen.queryByRole("button", { name: "plan.action" })).toBeNull();
  });

  it("a plan refusal that names no tier takes the matrix's tier and display name (#1645)", async () => {
    // A server predating #1644 names no tier; the operator's matrix does —
    // the first served tier with invitations, by its own display name.
    mockTiers = [
      { name: "basic", display_name: "M", team_invitations: false },
      { name: "team", display_name: "Team", team_invitations: true },
      { name: "pro", display_name: "L", team_invitations: true },
    ] as unknown as PlanTierFeature[];
    const serverText = "Feature 'team_invitations' not available.";
    vi.mocked(createInvitation).mockRejectedValue(
      refusal(403, "FEAT-001", serverText, {
        gate: "plan",
        feature: "team_invitations",
        required_plan: null,
        required_plan_display: null,
        current_plan: "basic",
      }),
    );
    await submitAdminInvite();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /plan\.description \{"plan":"Team"/,
    );
    expect(screen.queryByText(serverText)).toBeNull();
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

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("quota.title");
    expect(notice).toHaveTextContent('"current":5,"limit":5');
    expect(notice).toHaveTextContent('"feature":"features.members.singular"');
    expect(screen.queryByText(serverText)).toBeNull();
    expect(screen.queryByRole("button", { name: "quota.action" })).toBeNull();
  });

  it("does not render another quota with counts as the seat cap", async () => {
    const serverText = "Daily memory limit reached (100/100).";
    vi.mocked(createInvitation).mockRejectedValue(
      refusal(429, "QUOTA-001", serverText, {
        gate: "quota",
        quota_type: "memories_per_day",
        current: 100,
        limit: 100,
        required_plan: null,
        required_plan_display: null,
        current_plan: "pro",
      }),
    );
    await submitAdminInvite();

    expect(await screen.findByText(serverText)).toBeInTheDocument();
    expect(screen.queryByText("quota.title")).toBeNull();
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
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a new attempt clears the last refusal notice", async () => {
    vi.mocked(createInvitation).mockRejectedValueOnce(
      refusal(429, "QUOTA-001", "Member limit reached (5 seats).", {
        gate: "quota",
        quota_type: "members",
        current: 5,
        limit: 5,
        current_plan: "pro",
      }),
    );
    await submitAdminInvite();
    expect(await screen.findByRole("alert")).toHaveTextContent("quota.title");

    fireEvent.change(screen.getByPlaceholderText("emailPlaceholder"), {
      target: { value: "" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "createInvitation" }));
    });
    expect(screen.getByText("emailRequiredError")).toBeInTheDocument();
    expect(screen.queryByText("quota.title")).toBeNull();
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
