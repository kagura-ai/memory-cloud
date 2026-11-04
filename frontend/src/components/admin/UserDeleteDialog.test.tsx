/**
 * The admin user deletion dialog (#2420): confirm, then on the 409 a
 * successor picker built from its payload, then the DELETE again with the
 * selections.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/base";

const mockDeleteUser = vi.fn();
const mockToast = vi.fn();

vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    deleteUser: (...args: unknown[]) => mockDeleteUser(...args),
  };
});

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next-intl", () => ({
  useTranslations:
    (namespace: string) =>
    (key: string, values?: Record<string, string | number>) => {
      const fullKey = namespace ? `${namespace}.${key}` : key;
      return values && Object.keys(values).length > 0
        ? `${fullKey}:${JSON.stringify(values)}`
        : fullKey;
    },
}));

// Radix Select listens for pointer events the test DOM does not fire; a
// native <select> keeps the value / onValueChange contract the dialog uses.
type Children = { children?: React.ReactNode };
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
    disabled,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    children: React.ReactNode;
    disabled?: boolean;
  }) => (
    <select
      value={value ?? ""}
      disabled={disabled}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      <option value="" />
      {children}
    </select>
  ),
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: Children) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string } & Children) => (
    <option value={value}>{children}</option>
  ),
}));

import { UserDeleteDialog } from "./UserDeleteDialog";
import { USER_DELETE_DIALOG_TEST_IDS as IDS } from "./user-delete-dialog.testids";

const NS = "admin.users";

function transferRequired(detail: Record<string, unknown>): ApiError {
  return new ApiError({
    error: "HTTP-409",
    message: "Request failed",
    status: 409,
    details: { detail: { message: "Ownership transfer required", ...detail } },
  });
}

const member = (user_id: string, name: string, role: string) => ({
  user_id,
  name,
  email: `${user_id}@example.test`,
  role,
});

// The user owns ws-own (ctx-own was created there) and created ctx-other in
// ws-other, owned by owner-2; ctx-fixed-own and ctx-fixed-other are connector
// write targets.
const PICKER = {
  workspace_ids: ["ws-own"],
  context_ids: ["ctx-own", "ctx-other"],
  workspaces: [
    {
      id: "ws-own",
      name: "Own team",
      owner_user_id: "departing",
      members: [member("succ", "Successor", "admin"), member("view", "Viewer", "viewer")],
    },
    {
      id: "ws-other",
      name: "Other team",
      owner_user_id: "owner-2",
      members: [
        member("owner-2", "Other owner", "owner"),
        member("reach", "Reaching", "member"),
        member("view2", "Viewer two", "viewer"),
      ],
    },
  ],
  contexts: [
    {
      id: "ctx-own",
      name: "own-notes",
      display_name: "Own notes",
      workspace_id: "ws-own",
      is_private: false,
      candidate_user_ids: ["succ"],
    },
    {
      id: "ctx-other",
      name: "other-notes",
      display_name: null,
      workspace_id: "ws-other",
      is_private: false,
      candidate_user_ids: ["owner-2", "reach"],
    },
  ],
  owner_fixed_contexts: [
    {
      id: "ctx-fixed-own",
      name: "slack-own",
      display_name: null,
      workspace_id: "ws-own",
      successor_user_id: null,
    },
    {
      id: "ctx-fixed-other",
      name: "slack-other",
      display_name: null,
      workspace_id: "ws-other",
      successor_user_id: "owner-2",
    },
  ],
};

function renderDialog() {
  const onClose = vi.fn();
  const onDeleted = vi.fn();
  render(
    <UserDeleteDialog
      user={{ id: "departing", name: "Departing" }}
      onClose={onClose}
      onDeleted={onDeleted}
    />,
  );
  return { onClose, onDeleted };
}

function selectIn(testId: string): HTMLSelectElement {
  return within(screen.getByTestId(testId)).getByRole("combobox");
}

async function openPicker() {
  mockDeleteUser.mockRejectedValueOnce(transferRequired(PICKER));
  const handlers = renderDialog();
  fireEvent.click(screen.getByTestId(IDS.confirm));
  await screen.findByTestId(IDS.workspaceRow("ws-own"));
  return handlers;
}

beforeEach(() => {
  mockDeleteUser.mockReset();
  mockToast.mockReset();
});

describe("UserDeleteDialog (#2420)", () => {
  it("says owned workspaces and contexts are handed over, not deleted", () => {
    renderDialog();
    expect(screen.getByText(`${NS}.deleteDialog.handover`)).toBeTruthy();
    expect(screen.getByText(`${NS}.deleteDialog.deleted`)).toBeTruthy();
  });

  it("deletes a user who owns nothing with a bodyless DELETE", async () => {
    mockDeleteUser.mockResolvedValueOnce({});
    const { onDeleted } = renderDialog();
    fireEvent.click(screen.getByTestId(IDS.confirm));
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
    expect(mockDeleteUser).toHaveBeenCalledWith("departing");
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        description: `${NS}.messages.deleteSuccess:{"name":"Departing"}`,
      }),
    );
  });

  it("opens the picker on the 409 and resends the DELETE with the selections", async () => {
    const { onDeleted } = await openPicker();
    const submit = screen.getByTestId(IDS.submit) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    // A context of a workspace being handed over defaults to its successor.
    expect(selectIn(IDS.contextRow("ctx-own")).value).toBe("__workspace__");
    // Elsewhere only the candidates are offered, and nothing is preselected.
    const other = selectIn(IDS.contextRow("ctx-other"));
    expect(other.value).toBe("");
    const offered = Array.from(other.options)
      .map((o) => o.value)
      .filter(Boolean);
    expect(offered).toEqual(["owner-2", "reach"]);

    fireEvent.change(selectIn(IDS.workspaceRow("ws-own")), {
      target: { value: "succ" },
    });
    expect(submit.disabled).toBe(true);
    fireEvent.change(other, { target: { value: "reach" } });
    expect(submit.disabled).toBe(false);

    mockDeleteUser.mockResolvedValueOnce({});
    fireEvent.click(submit);
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
    expect(mockDeleteUser).toHaveBeenLastCalledWith("departing", {
      workspace_transfers: { "ws-own": "succ" },
      // "Same as the workspace successor" sends nothing for ctx-own.
      context_transfers: { "ctx-other": "reach" },
    });
  });

  it("shows a connector's write target fixed to the owner, with no choice", async () => {
    await openPicker();
    const theirs = screen.getByTestId(IDS.ownerFixedRow("ctx-fixed-other"));
    expect(within(theirs).queryByRole("combobox")).toBeNull();
    expect(
      within(theirs).getByText(
        `${NS}.deleteDialog.ownerFixedOwner:{"name":"Other owner"}`,
      ),
    ).toBeTruthy();
    const own = screen.getByTestId(IDS.ownerFixedRow("ctx-fixed-own"));
    expect(within(own).queryByRole("combobox")).toBeNull();
    expect(
      within(own).getByText(`${NS}.deleteDialog.ownerFixedFollows`),
    ).toBeTruthy();
    // It follows the workspace successor once one is picked.
    fireEvent.change(selectIn(IDS.workspaceRow("ws-own")), {
      target: { value: "succ" },
    });
    expect(
      within(own).getByText(
        `${NS}.deleteDialog.ownerFixedFollowsPicked:{"name":"Successor"}`,
      ),
    ).toBeTruthy();
  });

  it("refreshes the picker from a second 409, keeping the earlier picks", async () => {
    await openPicker();
    fireEvent.change(selectIn(IDS.workspaceRow("ws-own")), {
      target: { value: "succ" },
    });
    fireEvent.change(selectIn(IDS.contextRow("ctx-other")), {
      target: { value: "reach" },
    });
    // A context created in the meantime still needs a successor.
    mockDeleteUser.mockRejectedValueOnce(
      transferRequired({
        workspace_ids: [],
        context_ids: ["ctx-new"],
        workspaces: [PICKER.workspaces[1]],
        contexts: [
          {
            id: "ctx-new",
            name: "new-notes",
            display_name: "New notes",
            workspace_id: "ws-other",
            is_private: false,
            candidate_user_ids: ["owner-2"],
          },
        ],
        owner_fixed_contexts: PICKER.owner_fixed_contexts,
      }),
    );
    fireEvent.click(screen.getByTestId(IDS.submit));
    await screen.findByTestId(IDS.contextRow("ctx-new"));
    expect(screen.getByTestId(IDS.changed)).toBeTruthy();
    expect(selectIn(IDS.workspaceRow("ws-own")).value).toBe("succ");
    expect(selectIn(IDS.contextRow("ctx-other")).value).toBe("reach");
    const submit = screen.getByTestId(IDS.submit) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.change(selectIn(IDS.contextRow("ctx-new")), {
      target: { value: "owner-2" },
    });
    mockDeleteUser.mockResolvedValueOnce({});
    fireEvent.click(submit);
    await waitFor(() =>
      expect(mockDeleteUser).toHaveBeenLastCalledWith("departing", {
        workspace_transfers: { "ws-own": "succ" },
        context_transfers: { "ctx-other": "reach", "ctx-new": "owner-2" },
      }),
    );
  });

  it("shows a refusal at the confirm step inside the dialog, not as a toast", async () => {
    mockDeleteUser.mockRejectedValueOnce(
      new ApiError({
        error: "BILLING-005",
        message: "A paid subscription is active for this workspace.",
        status: 409,
        details: {},
      }),
    );
    const { onClose } = renderDialog();
    fireEvent.click(screen.getByTestId(IDS.confirm));
    const alert = await screen.findByTestId(IDS.error);
    expect(
      within(alert).getByText(
        `${NS}.messages.deleteErrorWithReason:{"reason":"A paid subscription is active for this workspace."}`,
      ),
    ).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    expect(mockToast).not.toHaveBeenCalled();
    expect(screen.queryByTestId(IDS.submit)).toBeNull();
    expect(
      (screen.getByTestId(IDS.confirm) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("keeps the picker and its picks on a refusal after it, saying why", async () => {
    const { onClose } = await openPicker();
    fireEvent.change(selectIn(IDS.workspaceRow("ws-own")), {
      target: { value: "succ" },
    });
    fireEvent.change(selectIn(IDS.contextRow("ctx-other")), {
      target: { value: "reach" },
    });
    mockDeleteUser.mockRejectedValueOnce(
      new ApiError({
        error: "HTTP-409",
        message: "Context successor must have workspace/context access",
        status: 409,
        details: { detail: "Context successor must have workspace/context access" },
      }),
    );
    fireEvent.click(screen.getByTestId(IDS.submit));
    const alert = await screen.findByTestId(IDS.error);
    expect(
      within(alert).getByText(
        `${NS}.messages.deleteErrorWithReason:{"reason":"Context successor must have workspace/context access"}`,
      ),
    ).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    expect(mockToast).not.toHaveBeenCalled();
    expect(selectIn(IDS.workspaceRow("ws-own")).value).toBe("succ");
    expect(selectIn(IDS.contextRow("ctx-other")).value).toBe("reach");
    expect((screen.getByTestId(IDS.submit) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("sends one DELETE for a double click", async () => {
    let finish: (value: unknown) => void = () => {};
    mockDeleteUser.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { onDeleted } = renderDialog();
    const confirm = screen.getByTestId(IDS.confirm);
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    finish({});
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
    expect(mockDeleteUser).toHaveBeenCalledTimes(1);
  });

  it("falls back to the counts when an older API sends ids only", async () => {
    mockDeleteUser.mockRejectedValueOnce(
      transferRequired({ workspace_ids: ["ws-1"], context_ids: [] }),
    );
    const { onClose } = renderDialog();
    fireEvent.click(screen.getByTestId(IDS.confirm));
    const alert = await screen.findByTestId(IDS.error);
    expect(
      within(alert).getByText(
        `${NS}.messages.deleteTransferRequired:{"workspaces":1,"contexts":0}`,
      ),
    ).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    expect(mockToast).not.toHaveBeenCalled();
    expect(screen.queryByTestId(IDS.submit)).toBeNull();
  });

  // #2640: a workspace nobody else is in (ws-solo, with ctx-solo created in
  // it) is deleted with the user on an explicit tick; ws-own still needs a
  // successor.
  const SOLO = {
    id: "ws-solo",
    name: "Solo",
    owner_user_id: "departing",
    members: [] as ReturnType<typeof member>[],
  };
  const SOLO_PICKER = {
    workspace_ids: ["ws-solo", "ws-own"],
    context_ids: ["ctx-solo"],
    workspaces: [SOLO, PICKER.workspaces[0]],
    contexts: [
      {
        id: "ctx-solo",
        name: "solo-notes",
        display_name: null,
        workspace_id: "ws-solo",
        is_private: false,
        candidate_user_ids: [],
      },
    ],
    owner_fixed_contexts: [],
  };

  async function openSoloPicker() {
    mockDeleteUser.mockRejectedValueOnce(transferRequired(SOLO_PICKER));
    const handlers = renderDialog();
    fireEvent.click(screen.getByTestId(IDS.confirm));
    await screen.findByTestId(IDS.workspaceRow("ws-solo"));
    return handlers;
  }

  it("offers to delete a workspace no member can take over, never by default", async () => {
    await openSoloPicker();
    const row = screen.getByTestId(IDS.workspaceRow("ws-solo"));
    expect(within(row).getByText(`${NS}.deleteDialog.noMembers`)).toBeTruthy();
    expect(within(row).getByText(`${NS}.deleteDialog.deleteWithUser`)).toBeTruthy();
    expect(within(row).queryByRole("combobox")).toBeNull();
    const tick = within(row).getByRole("checkbox");
    expect(tick.getAttribute("aria-checked")).toBe("false");
    // A context of it waits on its workspace while the workspace is kept.
    expect(screen.getByTestId(IDS.contextRow("ctx-solo"))).toBeTruthy();

    const submit = screen.getByTestId(IDS.submit) as HTMLButtonElement;
    fireEvent.change(selectIn(IDS.workspaceRow("ws-own")), {
      target: { value: "succ" },
    });
    expect(submit.disabled).toBe(true);
    fireEvent.click(screen.getByTestId(IDS.deleteWorkspace("ws-solo")));
    expect(tick.getAttribute("aria-checked")).toBe("true");
    // Its context goes with it: no row, no selection.
    expect(screen.queryByTestId(IDS.contextRow("ctx-solo"))).toBeNull();
    expect(submit.disabled).toBe(false);
  });

  it("sends the ticked workspace in delete_workspaces, not as a transfer", async () => {
    const { onDeleted } = await openSoloPicker();
    fireEvent.click(screen.getByTestId(IDS.deleteWorkspace("ws-solo")));
    const submit = screen.getByTestId(IDS.submit) as HTMLButtonElement;
    // A workspace with members still needs a successor.
    expect(submit.disabled).toBe(true);
    fireEvent.change(selectIn(IDS.workspaceRow("ws-own")), {
      target: { value: "succ" },
    });
    mockDeleteUser.mockResolvedValueOnce({});
    fireEvent.click(submit);
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
    expect(mockDeleteUser).toHaveBeenLastCalledWith("departing", {
      workspace_transfers: { "ws-own": "succ" },
      context_transfers: {},
      delete_workspaces: ["ws-solo"],
    });
  });

  it("clears the tick when a refreshed 409 gives the workspace a member", async () => {
    await openSoloPicker();
    fireEvent.click(screen.getByTestId(IDS.deleteWorkspace("ws-solo")));
    fireEvent.change(selectIn(IDS.workspaceRow("ws-own")), {
      target: { value: "succ" },
    });
    mockDeleteUser.mockRejectedValueOnce(
      transferRequired({
        workspace_ids: ["ws-solo"],
        context_ids: [],
        workspaces: [{ ...SOLO, members: [member("joined", "Joined", "member")] }],
        contexts: [],
        owner_fixed_contexts: [],
      }),
    );
    fireEvent.click(screen.getByTestId(IDS.submit));
    await screen.findByTestId(IDS.changed);
    expect(screen.queryByTestId(IDS.deleteWorkspace("ws-solo"))).toBeNull();
    const solo = selectIn(IDS.workspaceRow("ws-solo"));
    expect(solo.value).toBe("");
    const submit = screen.getByTestId(IDS.submit) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.change(solo, { target: { value: "joined" } });
    mockDeleteUser.mockResolvedValueOnce({});
    fireEvent.click(submit);
    await waitFor(() =>
      expect(mockDeleteUser).toHaveBeenLastCalledWith("departing", {
        workspace_transfers: { "ws-solo": "joined", "ws-own": "succ" },
        context_transfers: {},
      }),
    );
  });
});
