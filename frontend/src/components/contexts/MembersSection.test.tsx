/**
 * Tests for MembersSection (Issue #362).
 *
 * Covers the gate1 scope:
 *   - Visibility gating: (admin|member) × (shared|private) matrix
 *   - Add member flow (dialog + submit)
 *   - Role change with optimistic update + rollback on API error
 *   - Remove confirmation via AlertDialog
 *   - Loading / empty states
 */

import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MembersSection } from "./MembersSection";
import { ApiError } from "@/lib/api/base";
import type { Context } from "@/lib/types/context";
import type { ContextMember } from "@/lib/api/contexts";
import type { WorkspaceMember } from "@/lib/api/workspaces";
import { ContextRole } from "@/lib/auth/rbac";

// ---------- Mocks ------------------------------------------------------------

const mockListContextMembers = vi.fn();
const mockAddContextMember = vi.fn();
const mockUpdateContextMemberRole = vi.fn();
const mockRemoveContextMember = vi.fn();

vi.mock("@/lib/api/contexts", () => ({
  listContextMembers: (...a: unknown[]) => mockListContextMembers(...a),
  addContextMember: (...a: unknown[]) => mockAddContextMember(...a),
  updateContextMemberRole: (...a: unknown[]) =>
    mockUpdateContextMemberRole(...a),
  removeContextMember: (...a: unknown[]) => mockRemoveContextMember(...a),
}));

const mockListWorkspaceMembers = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  listMembers: (...a: unknown[]) => mockListWorkspaceMembers(...a),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const stableT = (key: string, vars?: Record<string, unknown>) => {
  if (vars && typeof vars.name === "string") return `${key}:${vars.name}`;
  if (vars && typeof vars.count === "number") return `${key}:${vars.count}`;
  return key;
};
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => stableT,
}));

const mockUseAuth = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockUseWorkspace = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockUseWorkspace(),
}));

// ---------- Helpers ----------------------------------------------------------

const CONTEXT_ID = "ctx-1";

function makeContext(overrides: Partial<Context> = {}): Context {
  return {
    id: CONTEXT_ID,
    name: "demo",
    display_name: "Demo Context",
    is_private: false,
    is_locked: false,
    is_default: false,
    created_at: "2026-04-01T00:00:00Z",
    updated_at: "2026-04-01T00:00:00Z",
    ...overrides,
  } as Context;
}

function makeMember(overrides: Partial<ContextMember> = {}): ContextMember {
  return {
    user_id: "user-editor",
    user_name: "Editor User",
    user_email: "editor@example.com",
    role: ContextRole.Editor,
    added_at: "2026-04-01T00:00:00Z",
    is_workspace_admin: false,
    ...overrides,
  };
}

function makeWorkspaceMember(
  overrides: Partial<WorkspaceMember> = {},
): WorkspaceMember {
  return {
    user_id: "user-candidate",
    user_name: "Candidate",
    user_email: "candidate@example.com",
    role: "member",
    joined_at: "2026-04-01T00:00:00Z",
    ...overrides,
  } as WorkspaceMember;
}

function setAuth(userId: string) {
  mockUseAuth.mockReturnValue({ user: { id: userId } });
}

function setWorkspace(role: string, id = "ws-1") {
  mockUseWorkspace.mockReturnValue({
    currentWorkspace: { id, current_user_role: role },
    currentWorkspaceId: id,
  });
}

// ---------- Tests ------------------------------------------------------------

describe("MembersSection — visibility gating", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockListContextMembers.mockResolvedValue([]);
    mockListWorkspaceMembers.mockResolvedValue([]);
    setAuth("admin-user");
  });

  it("renders nothing for private context even when user is workspace admin", () => {
    setWorkspace("admin");
    const { container } = render(
      <MembersSection
        contextId={CONTEXT_ID}
        context={makeContext({ is_private: true })}
      />,
    );
    expect(container.innerHTML).toBe("");
    expect(mockListContextMembers).not.toHaveBeenCalled();
  });

  it("renders nothing for member role on shared context", () => {
    setWorkspace("member");
    const { container } = render(
      <MembersSection contextId={CONTEXT_ID} context={makeContext()} />,
    );
    expect(container.innerHTML).toBe("");
    expect(mockListContextMembers).not.toHaveBeenCalled();
  });

  it("renders nothing for viewer role on shared context", () => {
    setWorkspace("viewer");
    const { container } = render(
      <MembersSection contextId={CONTEXT_ID} context={makeContext()} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("renders for admin on shared context and loads members", async () => {
    setWorkspace("admin");
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(mockListContextMembers).toHaveBeenCalledWith(CONTEXT_ID);
    });
    expect(screen.getByText("contextMembers")).toBeInTheDocument();
  });
});

describe("MembersSection — member list", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setWorkspace("owner");
    setAuth("owner-user");
  });

  it("shows empty state when no members are returned", async () => {
    mockListContextMembers.mockResolvedValue([]);
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByText("noMembersAssigned")).toBeInTheDocument();
    });
  });

  it("renders rows with role selectors for non-owner members", async () => {
    mockListContextMembers.mockResolvedValue([
      makeMember({
        user_id: "u1",
        user_email: "one@ex.com",
        role: ContextRole.Editor,
      }),
      makeMember({
        user_id: "u2",
        user_email: "two@ex.com",
        role: ContextRole.Owner,
        is_workspace_admin: false,
      }),
    ]);
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByTestId("member-row-u1")).toBeInTheDocument();
    });
    // u1 is editor → gets a role selector
    const row1 = screen.getByTestId("member-row-u1");
    expect(row1.querySelector("select")).toBeTruthy();
    // u2 is owner → no role selector (shown as badge)
    const row2 = screen.getByTestId("member-row-u2");
    expect(row2.querySelector("select")).toBeFalsy();
  });

  it("hides remove button for self rows", async () => {
    setAuth("u1");
    mockListContextMembers.mockResolvedValue([
      makeMember({
        user_id: "u1",
        user_email: "self@ex.com",
        role: ContextRole.Editor,
      }),
      makeMember({
        user_id: "u2",
        user_email: "other@ex.com",
        role: ContextRole.Editor,
      }),
    ]);
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByTestId("member-row-u1")).toBeInTheDocument();
    });
    const selfRow = screen.getByTestId("member-row-u1");
    expect(
      selfRow.querySelector("button[aria-label^='removeMember']"),
    ).toBeNull();
    const otherRow = screen.getByTestId("member-row-u2");
    expect(
      otherRow.querySelector("button[aria-label^='removeMember']"),
    ).toBeTruthy();
  });
});

describe("MembersSection — role change with rollback", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setWorkspace("owner");
    setAuth("owner-user");
  });

  it("optimistically updates and rolls back on API error", async () => {
    mockListContextMembers.mockResolvedValue([
      makeMember({ user_id: "u1", role: ContextRole.Editor }),
    ]);
    mockUpdateContextMemberRole.mockRejectedValue(
      new ApiError({
        status: 400,
        message: "Cannot demote last owner",
        details: { detail: "Cannot demote last owner" },
      }),
    );
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByTestId("member-row-u1")).toBeInTheDocument();
    });

    const select = screen
      .getByTestId("member-row-u1")
      .querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("editor");

    fireEvent.change(select, { target: { value: "viewer" } });

    await waitFor(() => {
      expect(mockUpdateContextMemberRole).toHaveBeenCalledWith(
        CONTEXT_ID,
        "u1",
        {
          role: "viewer",
        },
      );
    });

    // After rejection, the select value rolls back to editor.
    await waitFor(() => {
      const currentSelect = screen
        .getByTestId("member-row-u1")
        .querySelector("select") as HTMLSelectElement;
      expect(currentSelect.value).toBe("editor");
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
  });
});

describe("MembersSection — add member flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setWorkspace("admin");
    setAuth("admin-user");
    mockListContextMembers.mockResolvedValue([]);
    mockListWorkspaceMembers.mockResolvedValue([
      makeWorkspaceMember({ user_id: "cand-1", user_email: "c1@ex.com" }),
      makeWorkspaceMember({ user_id: "cand-2", user_email: "c2@ex.com" }),
    ]);
    mockAddContextMember.mockResolvedValue(makeMember({ user_id: "cand-1" }));
  });

  it("opens the add dialog, selects a candidate, and calls the API", async () => {
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByText("contextMembers")).toBeInTheDocument();
    });

    // Click the card-header "Add Member" button (first button with this label)
    const addButtons = screen.getAllByRole("button", { name: "addMember" });
    fireEvent.click(addButtons[0]);

    await waitFor(() => {
      expect(mockListWorkspaceMembers).toHaveBeenCalledWith("ws-1");
    });

    const dialog = await screen.findByRole("dialog");
    const memberSelect = within(dialog).getByLabelText(
      "selectWorkspaceMember",
    ) as HTMLSelectElement;
    fireEvent.change(memberSelect, { target: { value: "cand-1" } });

    // Click the dialog's submit button — within() scopes it to the dialog
    const submitButton = within(dialog).getByRole("button", {
      name: "addMember",
    });
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(mockAddContextMember).toHaveBeenCalledWith(CONTEXT_ID, {
        user_id: "cand-1",
        role: ContextRole.Editor,
      });
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "addMemberSuccess" }),
    );
  });
});

describe("MembersSection — member outside the context access list (#2327)", () => {
  // The body the API answers (pinned against the real ApiClient in
  // lib/api/base.test.ts): RES-002 with details.reason.
  const notInList = () =>
    new ApiError({
      status: 409,
      error: "RES-002",
      message:
        "This member's context access list does not include this context",
      details: {
        reason: "context_not_in_access_list",
        user_id: "cand-1",
        context_id: CONTEXT_ID,
      },
    });

  beforeEach(() => {
    vi.clearAllMocks();
    setWorkspace("admin");
    setAuth("admin-user");
    mockListContextMembers.mockResolvedValue([]);
    mockListWorkspaceMembers.mockResolvedValue([
      makeWorkspaceMember({ user_id: "cand-1", user_email: "c1@ex.com" }),
    ]);
  });

  async function submitAdd() {
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByText("contextMembers")).toBeInTheDocument();
    });
    fireEvent.click(screen.getAllByRole("button", { name: "addMember" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("selectWorkspaceMember"), {
      target: { value: "cand-1" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "addMember" }));
    return screen.findByRole("alertdialog");
  }

  it("asks to extend the list, then resends with the flag", async () => {
    mockAddContextMember
      .mockRejectedValueOnce(notInList())
      .mockResolvedValueOnce(
        makeMember({ user_id: "cand-1", access_list_extended: true }),
      );

    const confirm = await submitAdd();
    expect(
      within(confirm).getByText("extendAccessListTitle"),
    ).toBeInTheDocument();
    expect(
      within(confirm).getByText("extendAccessListDesc:c1@ex.com"),
    ).toBeInTheDocument();
    // The refusal is a question, not an error toast.
    expect(mockToast).not.toHaveBeenCalled();

    fireEvent.click(
      within(confirm).getByRole("button", { name: "extendAccessListConfirm" }),
    );

    await waitFor(() => {
      expect(mockAddContextMember).toHaveBeenLastCalledWith(CONTEXT_ID, {
        user_id: "cand-1",
        role: ContextRole.Editor,
        extend_access_list: true,
      });
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "addMemberSuccessListExtended" }),
      );
    });
    expect(mockAddContextMember).toHaveBeenCalledTimes(2);
    expect(mockAddContextMember).toHaveBeenNthCalledWith(1, CONTEXT_ID, {
      user_id: "cand-1",
      role: ContextRole.Editor,
    });
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
  });

  it("cancel sends nothing more", async () => {
    mockAddContextMember.mockRejectedValueOnce(notInList());

    const confirm = await submitAdd();
    fireEvent.click(within(confirm).getByRole("button", { name: "cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(mockAddContextMember).toHaveBeenCalledTimes(1);
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("keeps the toast for any other failure", async () => {
    mockAddContextMember.mockRejectedValueOnce(
      new ApiError({
        status: 409,
        error: "RES-002",
        message: "Resource conflict",
        details: { reason: "something_else" },
      }),
    );

    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByText("contextMembers")).toBeInTheDocument();
    });
    fireEvent.click(screen.getAllByRole("button", { name: "addMember" })[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("selectWorkspaceMember"), {
      target: { value: "cand-1" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "addMember" }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "addMemberFailed",
          variant: "destructive",
        }),
      );
    });
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});

describe("MembersSection — remove flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setWorkspace("owner");
    setAuth("owner-user");
    mockListContextMembers.mockResolvedValue([
      makeMember({
        user_id: "u-target",
        user_email: "target@ex.com",
        role: ContextRole.Editor,
      }),
    ]);
    mockRemoveContextMember.mockResolvedValue(undefined);
  });

  it("opens AlertDialog on trash click and calls the API on confirm", async () => {
    render(<MembersSection contextId={CONTEXT_ID} context={makeContext()} />);
    await waitFor(() => {
      expect(screen.getByTestId("member-row-u-target")).toBeInTheDocument();
    });

    const trashButton = screen
      .getByTestId("member-row-u-target")
      .querySelector("button[aria-label^='removeMember']") as HTMLButtonElement;
    fireEvent.click(trashButton);

    // Confirmation dialog is shown
    await waitFor(() => {
      expect(screen.getByText("removeMemberTitle")).toBeInTheDocument();
    });

    const confirmButton = screen.getByText("remove");
    fireEvent.click(confirmButton);

    await waitFor(() => {
      expect(mockRemoveContextMember).toHaveBeenCalledWith(
        CONTEXT_ID,
        "u-target",
      );
    });
  });
});
