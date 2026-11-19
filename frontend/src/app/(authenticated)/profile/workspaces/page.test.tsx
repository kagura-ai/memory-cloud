/**
 * Account settings > Workspace management (#2919).
 *
 * The API is mocked against the contract of #2918 (design 12.1). Test names
 * carry the acceptance bullet they cover:
 *   AC1 list, plan, slots, owned only, plan link
 *   AC2 cancellation: which to keep, preview before saving, optional
 *   AC3 reachable (the guard side is in layout.test.tsx)
 *   AC4 suspended workspace: reason, export, delete, resume, no-slot remedies, #1940
 *   AC5 resumed workspaces show as active with their own plan
 */

import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/base";
import type {
  SlotWorkspace,
  WorkspaceSlots,
} from "@/lib/api/workspaceSlots";

// ---------- Mocks ------------------------------------------------------------

vi.mock("next-intl", () => ({
  useTranslations:
    () => (key: string, values?: Record<string, unknown>) =>
      values ? `${key}|${JSON.stringify(values)}` : key,
}));
vi.mock("@/i18n", () => ({ useLocale: () => ({ locale: "en" }) }));

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

let mockInfo: { features: Record<string, boolean> } | null = null;
let mockInfoUnavailable = false;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => mockInfo,
  isSystemInfoUnavailable: () => mockInfoUnavailable,
}));

const mockSwitchWorkspace = vi.fn();
const mockRefreshWorkspaces = vi.fn();
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: "w1",
    switchWorkspace: mockSwitchWorkspace,
    refreshWorkspaces: mockRefreshWorkspaces,
  }),
}));
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { id: "u1", timezone: "UTC" } }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockGetSlots = vi.fn();
const mockPreview = vi.fn();
const mockSave = vi.fn();
const mockListContexts = vi.fn();
vi.mock("@/lib/api/workspaceSlots", () => ({
  getWorkspaceSlots: (...a: unknown[]) => mockGetSlots(...a),
  previewWorkspaceSlots: (...a: unknown[]) => mockPreview(...a),
  saveWorkspaceSelection: (...a: unknown[]) => mockSave(...a),
  listSlotWorkspaceContexts: (...a: unknown[]) => mockListContexts(...a),
}));
const mockDeleteWorkspace = vi.fn();
vi.mock("@/lib/api/workspaces", () => ({
  deleteWorkspace: (...a: unknown[]) => mockDeleteWorkspace(...a),
}));
const mockHandoff = vi.fn();
vi.mock("@/lib/api/billing", () => ({
  mintBillingHandoff: (...a: unknown[]) => mockHandoff(...a),
}));
vi.mock("@/components/contexts/ExportContextButton", () => ({
  ExportContextButton: ({ contextName }: { contextName: string }) => (
    <button type="button">{`export:${contextName}`}</button>
  ),
}));

import WorkspaceManagementPage from "./page";

// ---------- Fixtures ---------------------------------------------------------

function ws(
  id: string,
  name: string,
  over: Partial<SlotWorkspace> = {},
): SlotWorkspace {
  return {
    id,
    name,
    plan_name: "free",
    status: "active",
    suspended_at: null,
    under_billing_contract: false,
    is_current: false,
    created_at: "2026-03-02T09:14:00Z",
    ...over,
  };
}

/** The UI-check state: three owned, one slot after the term ended. */
function afterTermEnd(over: Partial<WorkspaceSlots> = {}): WorkspaceSlots {
  return {
    allowance: {
      cap: 1,
      base: 1,
      slot_bonus: 0,
      tier_grant: 0,
      tier: "free",
      next_tier: "pro",
      cap_on_next_tier: 3,
    },
    owned_count: 3,
    active_count: 1,
    active_slots: 1,
    contracted_count: 0,
    selection_required: true,
    selection: { workspace_ids: [], saved_at: null },
    version: 4,
    if_paid_plans_end: null,
    workspaces: [
      ws("w1", "Personal", { is_current: true }),
      ws("w2", "Research", {
        status: "slot_suspended",
        suspended_at: "2026-12-01T00:00:12Z",
      }),
      ws("w3", "Side project", {
        status: "slot_suspended",
        suspended_at: "2026-12-01T00:00:12Z",
      }),
    ],
    ...over,
  };
}

/** Before the term ends: Research is on a paid contract. */
function beforeTermEnd(over: Partial<WorkspaceSlots> = {}): WorkspaceSlots {
  return {
    allowance: {
      cap: 20,
      base: 1,
      slot_bonus: 0,
      tier_grant: 19,
      tier: "promax",
      next_tier: null,
      cap_on_next_tier: null,
    },
    owned_count: 3,
    active_count: 3,
    active_slots: 20,
    contracted_count: 1,
    selection_required: false,
    selection: { workspace_ids: [], saved_at: null },
    version: 7,
    if_paid_plans_end: { cap: 1, active: ["w1"], suspended: ["w2", "w3"] },
    workspaces: [
      ws("w1", "Personal", { is_current: true }),
      ws("w2", "Research", {
        plan_name: "promax",
        under_billing_contract: true,
      }),
      ws("w3", "Side project"),
    ],
    ...over,
  };
}

function apiError(status: number, error: string, details?: unknown) {
  return new ApiError({ status, error, message: `${error} message`, details });
}

const row = (id: string) => screen.getByTestId(`slot-row-${id}`);

async function renderLoaded(data: WorkspaceSlots) {
  mockGetSlots.mockResolvedValue(data);
  render(<WorkspaceManagementPage />);
  await screen.findByTestId(`slot-row-${data.workspaces[0].id}`);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockInfo = { features: { workspace_slots: true, plan_page: true } };
  mockInfoUnavailable = false;
  mockSwitchWorkspace.mockResolvedValue(undefined);
  mockRefreshWorkspaces.mockResolvedValue(undefined);
});

// ---------- Tests ------------------------------------------------------------

describe("states", () => {
  it("first paint is the table skeleton while the allowance loads", async () => {
    mockGetSlots.mockReturnValue(new Promise(() => {}));
    const { container } = render(<WorkspaceManagementPage />);
    expect(screen.getByRole("heading", { name: "title" })).toBeInTheDocument();
    expect(container.querySelectorAll(".animate-pulse").length).toBe(3);
    await waitFor(() => expect(mockGetSlots).toHaveBeenCalledTimes(1));
  });

  it("a failed load is an error banner", async () => {
    mockGetSlots.mockRejectedValue(new Error("boom"));
    render(<WorkspaceManagementPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("loadFailed");
  });

  it("an owner of nothing gets an empty state with a create action", async () => {
    mockGetSlots.mockResolvedValue(
      afterTermEnd({
        owned_count: 0,
        active_count: 0,
        selection_required: false,
        workspaces: [],
      }),
    );
    render(<WorkspaceManagementPage />);
    fireEvent.click(await screen.findByRole("button", { name: "empty.action" }));
    expect(screen.getByText("empty.title")).toBeInTheDocument();
    expect(mockPush).toHaveBeenCalledWith("/workspace/settings?create=true");
  });

  it("feature off: a not-available notice and no request", async () => {
    mockInfo = { features: { plan_page: true } };
    render(<WorkspaceManagementPage />);
    expect(await screen.findByText("unavailable")).toBeInTheDocument();
    expect(mockGetSlots).not.toHaveBeenCalled();
  });

  it("system info unreachable: a retry notice, never 'not available'", () => {
    mockInfo = { features: {} };
    mockInfoUnavailable = true;
    render(<WorkspaceManagementPage />);
    expect(screen.getByText(/lookupFailed/)).toBeInTheDocument();
    expect(screen.queryByText("unavailable")).toBeNull();
    expect(mockGetSlots).not.toHaveBeenCalled();
  });
});

describe("AC1 list, slots, owned only, plan link", () => {
  it("lists every owned workspace with its plan and status, and active slots against owned", async () => {
    await renderLoaded(afterTermEnd());
    expect(
      screen.getByText('summary.active|{"active":1,"slots":1}'),
    ).toBeInTheDocument();
    expect(screen.getByText('summary.owned|{"count":3}')).toBeInTheDocument();
    expect(within(row("w1")).getByText("Personal")).toBeInTheDocument();
    expect(within(row("w1")).getByText("status.active")).toBeInTheDocument();
    expect(within(row("w1")).getByText("table.current")).toBeInTheDocument();
    for (const id of ["w2", "w3"]) {
      expect(
        within(row(id)).getByText("status.slot_suspended"),
      ).toBeInTheDocument();
    }
  });

  it("says that shared workspaces the user joined do not count", async () => {
    await renderLoaded(afterTermEnd());
    expect(screen.getByText("summary.sharedNotCounted")).toBeInTheDocument();
  });

  it("plan link of an active workspace switches to it, then opens its plan page", async () => {
    await renderLoaded(beforeTermEnd());
    fireEvent.click(within(row("w2")).getByRole("button", { name: /changePlan/ }));
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan"),
    );
    expect(mockSwitchWorkspace).toHaveBeenCalledWith("w2");
    expect(mockHandoff).not.toHaveBeenCalled();
  });

  it("plan link of the current workspace opens the plan page without a switch", async () => {
    await renderLoaded(beforeTermEnd());
    fireEvent.click(within(row("w1")).getByRole("button", { name: /changePlan/ }));
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan"),
    );
    expect(mockSwitchWorkspace).not.toHaveBeenCalled();
  });

  it("plan link of a suspended workspace goes through the billing handoff, never a switch", async () => {
    mockHandoff.mockResolvedValue({ url: null });
    await renderLoaded(afterTermEnd());
    fireEvent.click(within(row("w2")).getByRole("button", { name: /changePlan/ }));
    await waitFor(() => expect(mockHandoff).toHaveBeenCalledWith("w2"));
    expect(mockSwitchWorkspace).not.toHaveBeenCalled();
    // No ready URL on this deployment: said, not silently ignored.
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "planUnavailable" }),
      ),
    );
  });

  it("no plan link when the deployment serves no plan page", async () => {
    mockInfo = { features: { workspace_slots: true } };
    await renderLoaded(afterTermEnd());
    expect(screen.queryByRole("button", { name: /changePlan/ })).toBeNull();
  });
});

describe("AC2 cancellation: which workspaces to keep", () => {
  it("appears only when ending the paid plans would suspend something", async () => {
    await renderLoaded(beforeTermEnd());
    expect(screen.getByText("cancellation.title")).toBeInTheDocument();
    expect(
      screen.getByText('cancellation.description|{"cap":1}'),
    ).toBeInTheDocument();
    expect(screen.getByText("cancellation.optional")).toBeInTheDocument();
    expect(screen.getByText("keep.question")).toBeInTheDocument();
  });

  it.each([
    ["nothing is under a contract", null],
    ["everything would still fit", { cap: 3, active: ["w1", "w2", "w3"], suspended: [] }],
  ])("is absent when %s", async (_label, ifEnd) => {
    await renderLoaded(beforeTermEnd({ if_paid_plans_end: ifEnd }));
    expect(screen.queryByText("cancellation.title")).toBeNull();
    expect(screen.queryByText("keep.question")).toBeNull();
  });

  it("keyboard selection: a box takes focus and toggles, the cap disables the rest and the live text says the cap", async () => {
    await renderLoaded(beforeTermEnd());
    const personal = screen.getByRole("checkbox", { name: /Personal/ });
    const research = screen.getByRole("checkbox", { name: /Research/ });
    const side = screen.getByRole("checkbox", { name: /Side project/ });
    const live = screen.getByText('keep.count|{"count":1,"max":1}');
    expect(live).toHaveAttribute("role", "status");
    expect(live).toHaveAttribute("aria-live", "polite");

    // At the cap (1): the kept one is checked, the others are disabled and
    // so out of the tab order — the live text names the cap.
    expect(personal).toHaveAttribute("aria-checked", "true");
    expect(research).toBeDisabled();
    expect(side).toBeDisabled();

    // Space on a focused button-checkbox activates it (a click event).
    personal.focus();
    expect(personal).toHaveFocus();
    fireEvent.click(personal);
    expect(personal).toHaveAttribute("aria-checked", "false");
    expect(research).not.toBeDisabled();
    expect(side).not.toBeDisabled();
    expect(live).toHaveTextContent('keep.count|{"count":0,"max":1}');

    side.focus();
    fireEvent.click(side);
    expect(side).toHaveAttribute("aria-checked", "true");
    expect(personal).toBeDisabled();
    expect(live).toHaveTextContent('keep.count|{"count":1,"max":1}');
  });

  it("preview shows which stay active and which will be suspended, and saves nothing", async () => {
    mockPreview.mockResolvedValue({
      scenario: "paid_plans_end",
      cap: 1,
      active_slots: 1,
      active: ["w3"],
      suspended: ["w1", "w2"],
      unmet: [],
      changes: { suspend: ["w1", "w2"], resume: ["w3"] },
    });
    await renderLoaded(beforeTermEnd());
    fireEvent.click(screen.getByRole("checkbox", { name: /Personal/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Side project/ }));
    const save = screen.getByRole("button", { name: "keep.save" });
    expect(save).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "keep.preview" }));
    const result = await screen.findByRole("region", { name: "keep.previewTitle" });
    expect(mockPreview).toHaveBeenCalledWith(["w3"], "paid_plans_end");
    const [stays, suspended] = within(result).getAllByRole("list");
    expect(within(stays).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Side project",
    ]);
    expect(
      within(suspended).getAllByRole("listitem").map((li) => li.textContent),
    ).toEqual(["Personal", "Research"]);
    expect(mockSave).not.toHaveBeenCalled();
    expect(save).not.toBeDisabled();
  });

  it("save sends the selection with expected_version and shows the saved state", async () => {
    mockPreview.mockResolvedValue({
      scenario: "paid_plans_end",
      cap: 1,
      active_slots: 1,
      active: ["w1"],
      suspended: ["w2", "w3"],
      unmet: [],
      changes: { suspend: ["w2", "w3"], resume: [] },
    });
    const saved = beforeTermEnd({
      version: 8,
      selection: { workspace_ids: ["w1"], saved_at: "2026-11-19T00:00:00Z" },
    });
    mockSave.mockResolvedValue(saved);
    await renderLoaded(beforeTermEnd());
    fireEvent.click(screen.getByRole("button", { name: "keep.preview" }));
    await screen.findByRole("region", { name: "keep.previewTitle" });
    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith(["w1"], 7));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith({ title: "keep.saved" }),
    );
    expect(mockRefreshWorkspaces).toHaveBeenCalled();
  });

  it("a stale version (SLOT-003) refetches and asks to review again", async () => {
    mockPreview.mockResolvedValue({
      scenario: "paid_plans_end",
      cap: 1,
      active_slots: 1,
      active: ["w1"],
      suspended: ["w2", "w3"],
      unmet: [],
      changes: { suspend: [], resume: [] },
    });
    mockSave.mockRejectedValue(
      apiError(409, "SLOT-003", { expected_version: 7, current_version: 8 }),
    );
    await renderLoaded(beforeTermEnd());
    fireEvent.click(screen.getByRole("button", { name: "keep.preview" }));
    await screen.findByRole("region", { name: "keep.previewTitle" });
    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    expect(await screen.findByText("stale")).toBeInTheDocument();
    await waitFor(() => expect(mockGetSlots).toHaveBeenCalledTimes(2));
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
  });
});

describe("AC3 selection required after the term ended", () => {
  it("shows the selection-required notice and the choice for the current allowance", async () => {
    await renderLoaded(afterTermEnd());
    expect(screen.getByText("selectionRequired.title")).toBeInTheDocument();
    expect(screen.getByText("current.title")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: /Personal/ }),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("no choice section while nothing is suspended and no contract could end", async () => {
    await renderLoaded(
      beforeTermEnd({ if_paid_plans_end: null, contracted_count: 0 }),
    );
    expect(screen.queryByText("current.title")).toBeNull();
    expect(screen.queryByText("selectionRequired.title")).toBeNull();
  });
});

describe("AC4 a suspended workspace", () => {
  it("explains the reason and offers resume, export and delete; an active one has none", async () => {
    await renderLoaded(afterTermEnd());
    const research = within(row("w2"));
    expect(research.getByText("reason")).toBeInTheDocument();
    expect(research.getByText(/status\.suspendedOn/)).toBeInTheDocument();
    for (const name of ["actions.resume", "actions.export", "actions.delete"]) {
      expect(research.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(within(row("w1")).queryByRole("button", { name: "actions.resume" })).toBeNull();
  });

  it("an active workspace under a paid subscription says it cannot be deleted (#1940)", async () => {
    await renderLoaded(beforeTermEnd());
    expect(within(row("w2")).getByText("contractNote")).toBeInTheDocument();
    expect(within(row("w3")).queryByText("contractNote")).toBeNull();
  });

  it("export lists the contexts with a button each and says other members' private contexts are left out", async () => {
    mockListContexts.mockResolvedValue({
      workspace_id: "w2",
      contexts: [
        { id: "c1", name: "Papers", is_private: false },
        { id: "c2", name: "My drafts", is_private: true },
      ],
    });
    await renderLoaded(afterTermEnd());
    fireEvent.click(within(row("w2")).getByRole("button", { name: "actions.export" }));
    const dialog = await screen.findByRole("dialog");
    expect(mockListContexts).toHaveBeenCalledWith("w2");
    expect(await within(dialog).findByRole("button", { name: "export:Papers" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "export:My drafts" })).toBeInTheDocument();
    expect(within(dialog).getByText("export.private")).toBeInTheDocument();
    expect(within(dialog).getByText("export.privateNote")).toBeInTheDocument();
  });

  it("export of a workspace without contexts is an empty state", async () => {
    mockListContexts.mockResolvedValue({ workspace_id: "w3", contexts: [] });
    await renderLoaded(afterTermEnd());
    fireEvent.click(within(row("w3")).getByRole("button", { name: "actions.export" }));
    expect(await screen.findByText("export.emptyTitle")).toBeInTheDocument();
  });

  it("delete asks first, deletes, and refreshes", async () => {
    mockDeleteWorkspace.mockResolvedValue(undefined);
    await renderLoaded(afterTermEnd());
    fireEvent.click(within(row("w3")).getByRole("button", { name: "actions.delete" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("deleteConfirmTitle")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "deleteWorkspace" }));
    await waitFor(() => expect(mockDeleteWorkspace).toHaveBeenCalledWith("w3"));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(mockGetSlots).toHaveBeenCalledTimes(2);
    expect(mockRefreshWorkspaces).toHaveBeenCalled();
  });

  it("a running paid subscription (409 BILLING-005) is said inside the delete dialog", async () => {
    mockDeleteWorkspace.mockRejectedValue(
      apiError(409, "BILLING-005", { workspace_ids: ["w3"] }),
    );
    await renderLoaded(afterTermEnd());
    fireEvent.click(within(row("w3")).getByRole("button", { name: "actions.delete" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "deleteWorkspace" }));
    expect(
      await within(dialog).findByText("deleteBlockedBySubscription"),
    ).toBeInTheDocument();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("resume with a free slot says so and saves the workspace first", async () => {
    const data = afterTermEnd({ active_slots: 2, active_count: 1 });
    mockPreview.mockResolvedValue({
      scenario: "current",
      cap: 2,
      active_slots: 2,
      active: ["w2", "w1"],
      suspended: ["w3"],
      unmet: [],
      changes: { suspend: [], resume: ["w2"] },
    });
    mockSave.mockResolvedValue(afterTermEnd({ version: 5 }));
    await renderLoaded(data);
    fireEvent.click(within(row("w2")).getByRole("button", { name: "actions.resume" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(
      await within(dialog).findByText('resume.freeSlot|{"name":"Research"}'),
    ).toBeInTheDocument();
    expect(mockPreview).toHaveBeenCalledWith(["w2", "w1"], "current");
    fireEvent.click(within(dialog).getByRole("button", { name: "resume.confirm" }));
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith(["w2", "w1"], 4));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("resume as a swap names the workspace that is suspended instead", async () => {
    mockPreview.mockResolvedValue({
      scenario: "current",
      cap: 1,
      active_slots: 1,
      active: ["w2"],
      suspended: ["w1", "w3"],
      unmet: [],
      changes: { suspend: ["w1"], resume: ["w2"] },
    });
    await renderLoaded(afterTermEnd());
    fireEvent.click(within(row("w2")).getByRole("button", { name: "actions.resume" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(
      await within(dialog).findByText('resume.swap|{"name":"Research"}'),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("listitem")).toHaveTextContent("Personal");
  });

  it("no slot can be freed (SLOT-002): the remedies name the next plan and deleting", async () => {
    mockPreview.mockResolvedValue({
      scenario: "current",
      cap: 1,
      active_slots: 1,
      active: ["w2"],
      suspended: ["w1", "w3"],
      unmet: [],
      changes: { suspend: ["w1"], resume: ["w2"] },
    });
    mockSave.mockRejectedValue(
      apiError(409, "SLOT-002", {
        cap: 1,
        active_slots: 1,
        active_count: 1,
        contracted_count: 1,
        next_tier: "pro",
        cap_on_next_tier: 3,
        unmet: ["w2"],
      }),
    );
    await renderLoaded(afterTermEnd());
    fireEvent.click(within(row("w2")).getByRole("button", { name: "actions.resume" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(
      await within(dialog).findByRole("button", { name: "resume.confirm" }),
    );
    expect(await within(dialog).findByText("full.title")).toBeInTheDocument();
    expect(within(dialog).getByText(/^full\.upgrade\|/)).toHaveTextContent('"cap":3');
    expect(within(dialog).getByText("full.delete")).toBeInTheDocument();
  });

  it("no slot can be freed on the top plan: no upgrade sentence", async () => {
    mockPreview.mockResolvedValue({
      scenario: "current",
      cap: 1,
      active_slots: 1,
      active: ["w1"],
      suspended: ["w2", "w3"],
      unmet: ["w2"],
      changes: { suspend: [], resume: [] },
    });
    await renderLoaded(
      afterTermEnd({
        allowance: {
          cap: 1,
          base: 1,
          slot_bonus: 0,
          tier_grant: 0,
          tier: "promax",
          next_tier: null,
          cap_on_next_tier: null,
        },
      }),
    );
    fireEvent.click(within(row("w2")).getByRole("button", { name: "actions.resume" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(await within(dialog).findByText("full.title")).toBeInTheDocument();
    expect(within(dialog).queryByText(/^full\.upgrade/)).toBeNull();
    expect(within(dialog).getByText("full.delete")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "resume.confirm" })).toBeNull();
  });
});

describe("AC5 resumed after an upgrade", () => {
  it("a refetch on window focus shows the resumed workspaces as active with their own plans", async () => {
    await renderLoaded(afterTermEnd());
    mockGetSlots.mockResolvedValue(
      afterTermEnd({
        active_count: 3,
        active_slots: 3,
        selection_required: false,
        workspaces: [
          ws("w1", "Personal", { is_current: true }),
          ws("w2", "Research", { plan_name: "pro" }),
          ws("w3", "Side project"),
        ],
      }),
    );
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() =>
      expect(
        screen.getByText('summary.active|{"active":3,"slots":3}'),
      ).toBeInTheDocument(),
    );
    for (const id of ["w1", "w2", "w3"]) {
      expect(within(row(id)).getByText("status.active")).toBeInTheDocument();
    }
    expect(screen.queryByRole("button", { name: "actions.resume" })).toBeNull();
    expect(screen.queryByText("selectionRequired.title")).toBeNull();
  });
});
