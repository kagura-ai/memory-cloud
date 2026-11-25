/**
 * Account settings > Workspace management (#2919).
 *
 * The API is mocked against the contract of #2918 (design 12.1). Test names
 * carry the acceptance bullet they cover:
 *   AC1 list, plan, slots, owned only, plan link
 *   AC2 cancellation: which to keep, preview before saving, optional
 *   AC3 selection after the term ended (the guard side is in layout.test.tsx)
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
import { formatDate } from "@/lib/utils/datetime";
import type {
  SlotWorkspace,
  WorkspaceSlotPreview,
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
let mockListed: { id: string; slot_suspended?: boolean }[] = [];
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspaceId: "w1",
    workspaces: mockListed,
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
  ExportContextButton: ({
    contextName,
    ariaLabel,
  }: {
    contextName: string;
    ariaLabel?: string;
  }) => (
    <button type="button" aria-label={ariaLabel}>
      {`export:${contextName}`}
    </button>
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
  mockListed = [];
  mockRefreshWorkspaces.mockResolvedValue(undefined);
});

// ---------- Helpers ----------------------------------------------------------

function previewOf(over: Partial<WorkspaceSlotPreview>): WorkspaceSlotPreview {
  return {
    scenario: "current",
    cap: 1,
    active_slots: 1,
    active: [],
    suspended: [],
    unmet: [],
    changes: { suspend: [], resume: [] },
    ...over,
  };
}

const named = (key: string, name: string) =>
  `actions.${key}|${JSON.stringify({ name })}`;
const rowButton = (id: string, key: string, name: string) =>
  within(row(id)).getByRole("button", { name: named(key, name) });
const listItems = (name: string) =>
  within(screen.getByRole("list", { name }))
    .getAllByRole("listitem")
    .map((li) => li.textContent);

/**
 * Space on a focused button-checkbox as a browser handles it: keydown and
 * keyup, then the button's activation (a click) unless the keydown's default
 * was prevented. happy-dom dispatches the key events but has no activation
 * behaviour, so the last step is the browser's, written out.
 */
function pressSpace(el: HTMLElement) {
  el.focus();
  const notPrevented = fireEvent.keyDown(el, { key: " ", code: "Space" });
  fireEvent.keyUp(el, { key: " ", code: "Space" });
  if (notPrevented) fireEvent.click(el);
}

async function previewAndWait() {
  fireEvent.click(screen.getByRole("button", { name: "keep.preview" }));
  return screen.findByRole("region", { name: "keep.previewTitle" });
}

// ---------- Tests ------------------------------------------------------------

describe("states", () => {
  it("first paint is the table skeleton while the allowance loads", async () => {
    mockGetSlots.mockReturnValue(new Promise(() => {}));
    const { container } = render(<WorkspaceManagementPage />);
    expect(screen.getByRole("heading", { name: "title" })).toBeInTheDocument();
    expect(container.querySelectorAll(".animate-pulse").length).toBe(3);
    await waitFor(() => expect(mockGetSlots).toHaveBeenCalledTimes(1));
  });

  it("a failed load is an error banner with a retry", async () => {
    mockGetSlots.mockRejectedValueOnce(new Error("boom"));
    render(<WorkspaceManagementPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("loadFailed");
    mockGetSlots.mockResolvedValue(afterTermEnd());
    fireEvent.click(screen.getByRole("button", { name: "retry" }));
    expect(await screen.findByTestId("slot-row-w1")).toBeInTheDocument();
    expect(screen.queryByText("loadFailed")).toBeNull();
  });

  it("a 404 from the API although the flag is on reads as not available, not as an error", async () => {
    mockGetSlots.mockRejectedValue(apiError(404, "HTTP-404"));
    render(<WorkspaceManagementPage />);
    expect(await screen.findByText("unavailable")).toBeInTheDocument();
    expect(screen.queryByText("loadFailed")).toBeNull();
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
    fireEvent.click(
      await screen.findByRole("button", { name: "empty.action" }),
    );
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

  it("the table is named by its section heading", async () => {
    await renderLoaded(afterTermEnd());
    expect(
      screen.getByRole("table", { name: "table.caption" }),
    ).toBeInTheDocument();
  });

  it("says that shared workspaces the user joined do not count", async () => {
    await renderLoaded(afterTermEnd());
    expect(screen.getByText("summary.sharedNotCounted")).toBeInTheDocument();
  });

  it("plan link of an active workspace switches to it, then opens its plan page", async () => {
    await renderLoaded(beforeTermEnd());
    fireEvent.click(rowButton("w2", "changePlanNamed", "Research"));
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan"),
    );
    expect(mockSwitchWorkspace).toHaveBeenCalledWith("w2");
    expect(mockHandoff).not.toHaveBeenCalled();
  });

  it("plan link of the current workspace opens the plan page without a switch", async () => {
    await renderLoaded(beforeTermEnd());
    fireEvent.click(rowButton("w1", "changePlanNamed", "Personal"));
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan"),
    );
    expect(mockSwitchWorkspace).not.toHaveBeenCalled();
  });

  it("plan link of a suspended workspace asks first; Continue hands off to billing, never a switch", async () => {
    mockHandoff.mockReturnValue(new Promise(() => {}));
    await renderLoaded(afterTermEnd());
    const opener = rowButton("w2", "changePlanNamed", "Research");
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("alertdialog");
    expect(
      within(dialog).getByText('suspendedPlan.title|{"name":"Research"}'),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("suspendedPlan.body")).toBeInTheDocument();
    expect(mockHandoff).not.toHaveBeenCalled();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "suspendedPlan.continue" }),
    );
    await waitFor(() => expect(mockHandoff).toHaveBeenCalledWith("w2"));
    expect(mockSwitchWorkspace).not.toHaveBeenCalled();
  });

  it("Cancel closes the plan dialog without a handoff and returns focus", async () => {
    await renderLoaded(afterTermEnd());
    const opener = rowButton("w2", "changePlanNamed", "Research");
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(mockHandoff).not.toHaveBeenCalled();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it.each([
    ["no billing URL on this deployment", () => mockHandoff.mockResolvedValue({ url: null }), "planUnavailable"],
    ["billing not configured (503)", () => mockHandoff.mockRejectedValue(apiError(503, "BILLING-002")), "planUnavailable"],
    ["another refusal", () => mockHandoff.mockRejectedValue(apiError(429, "RATE-001")), "RATE-001 message"],
  ])("a handoff failure (%s) is said inside the plan dialog", async (_label, arrange, text) => {
    arrange();
    await renderLoaded(afterTermEnd());
    fireEvent.click(rowButton("w2", "changePlanNamed", "Research"));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "suspendedPlan.continue" }),
    );
    expect(await within(dialog).findByText(text)).toBeInTheDocument();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("an active workspace's plan link opens no dialog", async () => {
    await renderLoaded(beforeTermEnd());
    fireEvent.click(rowButton("w3", "changePlanNamed", "Side project"));
    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan"),
    );
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("a refused switch opens no plan page", async () => {
    mockSwitchWorkspace.mockRejectedValue(new Error("refused"));
    await renderLoaded(beforeTermEnd());
    fireEvent.click(rowButton("w2", "changePlanNamed", "Research"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive", title: "refused" }),
      ),
    );
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("a workspace the workspace list already marks suspended goes through the billing handoff", async () => {
    mockListed = [{ id: "w3", slot_suspended: true }];
    mockHandoff.mockResolvedValue({ url: null });
    await renderLoaded(beforeTermEnd());
    fireEvent.click(rowButton("w3", "changePlanNamed", "Side project"));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "suspendedPlan.continue" }),
    );
    await waitFor(() => expect(mockHandoff).toHaveBeenCalledWith("w3"));
    expect(mockSwitchWorkspace).not.toHaveBeenCalled();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("no plan link when the deployment serves no plan page", async () => {
    mockInfo = { features: { workspace_slots: true } };
    await renderLoaded(afterTermEnd());
    expect(
      screen.queryByRole("button", { name: /changePlanNamed/ }),
    ).toBeNull();
  });
});

describe("AC2 cancellation: which workspaces to keep", () => {
  it("with nothing suspended, the cancellation block carries the selection control", async () => {
    await renderLoaded(beforeTermEnd());
    expect(screen.getByText("cancellation.title")).toBeInTheDocument();
    expect(
      screen.getByText('cancellation.description|{"cap":1}'),
    ).toBeInTheDocument();
    expect(screen.getByText("cancellation.optional")).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "keep.questionAfterEnd" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("current.title")).toBeNull();
  });

  it.each([
    ["nothing is under a contract", null],
    [
      "everything would still fit",
      { cap: 3, active: ["w1", "w2", "w3"], suspended: [] },
    ],
  ])("is absent when %s", async (_label, ifEnd) => {
    await renderLoaded(beforeTermEnd({ if_paid_plans_end: ifEnd }));
    expect(screen.queryByText("cancellation.title")).toBeNull();
    expect(screen.queryByRole("group")).toBeNull();
  });

  it("the fieldset is described by the count, which names the cap", async () => {
    await renderLoaded(beforeTermEnd());
    const group = screen.getByRole("group", { name: "keep.questionAfterEnd" });
    const count = screen.getByText('keep.count|{"count":1,"max":1}');
    expect(group).toHaveAttribute("aria-describedby", count.id);
    expect(count).toHaveAttribute("role", "status");
    expect(count).toHaveAttribute("aria-live", "polite");
    // The count and the buttons sit inside the group.
    expect(group).toContainElement(count);
    expect(group).toContainElement(
      screen.getByRole("button", { name: "keep.preview" }),
    );
  });

  it("keyboard selection with Space: the cap disables the rest and the live text follows", async () => {
    await renderLoaded(beforeTermEnd());
    const personal = screen.getByRole("checkbox", { name: /Personal/ });
    const research = screen.getByRole("checkbox", { name: /Research/ });
    const side = screen.getByRole("checkbox", { name: /Side project/ });

    expect(personal).toHaveAttribute("aria-checked", "true");
    expect(research).toBeDisabled();
    expect(side).toBeDisabled();

    pressSpace(personal);
    expect(personal).toHaveFocus();
    expect(personal).toHaveAttribute("aria-checked", "false");
    expect(research).not.toBeDisabled();
    expect(side).not.toBeDisabled();
    expect(
      screen.getByText('keep.count|{"count":0,"max":1}'),
    ).toBeInTheDocument();

    pressSpace(side);
    expect(side).toHaveFocus();
    expect(side).toHaveAttribute("aria-checked", "true");
    expect(personal).toBeDisabled();
    expect(
      screen.getByText('keep.count|{"count":1,"max":1}'),
    ).toBeInTheDocument();
  });

  it("preview lists what stays active and what will be suspended, moves focus there and saves nothing", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        scenario: "paid_plans_end",
        active: ["w3"],
        suspended: ["w1", "w2"],
        changes: { suspend: ["w1", "w2"], resume: [] },
      }),
    );
    await renderLoaded(beforeTermEnd());
    fireEvent.click(screen.getByRole("checkbox", { name: /Personal/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Side project/ }));
    const save = screen.getByRole("button", { name: "keep.save" });
    expect(save).toBeDisabled();
    expect(screen.getByText("keep.previewHint")).toBeInTheDocument();

    await previewAndWait();
    expect(mockPreview).toHaveBeenCalledWith(["w3"], "paid_plans_end");
    expect(
      screen.getByRole("heading", { name: "keep.previewTitle" }),
    ).toHaveFocus();
    expect(listItems("keep.staysActive")).toEqual(["Side project"]);
    expect(listItems("keep.willBeSuspended")).toEqual(["Personal", "Research"]);
    // "No slot can be freed" belongs to the current scenario only.
    expect(screen.queryByText("keep.unmet")).toBeNull();
    expect(mockSave).not.toHaveBeenCalled();
    expect(save).not.toBeDisabled();
    expect(screen.queryByText("keep.previewHint")).toBeNull();
  });

  it("a preview answering after the selection changed never enables Save", async () => {
    let answer!: (p: WorkspaceSlotPreview) => void;
    mockPreview.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    await renderLoaded(beforeTermEnd());
    fireEvent.click(screen.getByRole("button", { name: "keep.preview" }));
    // Selection A = [w1] is being previewed; switch to B = [w3].
    fireEvent.click(screen.getByRole("checkbox", { name: /Personal/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Side project/ }));
    await act(async () => {
      answer(
        previewOf({
          scenario: "paid_plans_end",
          active: ["w1"],
          suspended: ["w2", "w3"],
          changes: { suspend: ["w2", "w3"], resume: [] },
        }),
      );
    });
    expect(screen.getByRole("button", { name: "keep.save" })).toBeDisabled();
    expect(
      screen.queryByRole("region", { name: "keep.previewTitle" }),
    ).toBeNull();
  });

  it("save sends the selection with expected_version and moves focus to the section heading", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        scenario: "paid_plans_end",
        active: ["w1"],
        suspended: ["w2", "w3"],
        changes: { suspend: ["w2", "w3"], resume: [] },
      }),
    );
    const saved = beforeTermEnd({
      version: 8,
      selection: { workspace_ids: ["w1"], saved_at: "2026-11-19T00:00:00Z" },
    });
    mockSave.mockResolvedValue(saved);
    await renderLoaded(beforeTermEnd());
    await previewAndWait();
    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith(["w1"], 7));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith({ title: "keep.saved" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "cancellation.title" }),
      ).toHaveFocus(),
    );
    expect(mockRefreshWorkspaces).toHaveBeenCalledWith({ quiet: true });
  });

  it("a stale version (SLOT-003) refetches, and says the list is up to date only after it is", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        scenario: "paid_plans_end",
        active: ["w1"],
        suspended: ["w2", "w3"],
      }),
    );
    mockSave.mockRejectedValue(
      apiError(409, "SLOT-003", { expected_version: 7, current_version: 8 }),
    );
    await renderLoaded(beforeTermEnd());
    let refetched!: (d: WorkspaceSlots) => void;
    mockGetSlots.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          refetched = resolve;
        }),
    );
    await previewAndWait();
    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    expect(await screen.findByText("stale")).toBeInTheDocument();
    expect(screen.queryByText("staleRefreshed")).toBeNull();
    await act(async () => {
      refetched(beforeTermEnd({ version: 8 }));
    });
    expect(await screen.findByText("staleRefreshed")).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "cancellation.title" }),
      ).toHaveFocus(),
    );
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
  });
});

describe("AC3 the choice for the current allowance", () => {
  it("the UI check: three owned, one slot — pick another, preview, save", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w2"],
        suspended: ["w1", "w3"],
        changes: { suspend: ["w1"], resume: ["w2"] },
      }),
    );
    mockSave.mockResolvedValue(afterTermEnd({ version: 5 }));
    await renderLoaded(afterTermEnd());
    expect(screen.getByText("selectionRequired.title")).toBeInTheDocument();
    const group = screen.getByRole("group", { name: "keep.question" });
    pressSpace(within(group).getByRole("checkbox", { name: /Personal/ }));
    pressSpace(within(group).getByRole("checkbox", { name: /Research/ }));
    expect(
      screen.getByText('keep.count|{"count":1,"max":1}'),
    ).toBeInTheDocument();

    await previewAndWait();
    expect(mockPreview).toHaveBeenCalledWith(["w2"], "current");
    expect(listItems("keep.staysActive")).toEqual(["Research"]);
    expect(listItems("keep.willBeSuspended")).toEqual(["Personal"]);
    expect(listItems("keep.staysSuspended")).toEqual(["Side project"]);

    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith(["w2"], 4));
  });

  it("while anything is suspended there is one selection control; the end-of-term block is information only", async () => {
    await renderLoaded(
      afterTermEnd({
        if_paid_plans_end: { cap: 1, active: ["w1"], suspended: ["w2", "w3"] },
      }),
    );
    expect(screen.getAllByRole("group")).toHaveLength(1);
    expect(
      screen.getByRole("group", { name: "keep.question" }),
    ).toBeInTheDocument();
    expect(screen.getByText("cancellation.title")).toBeInTheDocument();
    expect(screen.getByText("cancellation.wouldSuspend")).toBeInTheDocument();
    expect(screen.queryByText("cancellation.optional")).toBeNull();
    expect(
      screen.getAllByRole("button", { name: "keep.preview" }),
    ).toHaveLength(1);
  });

  it("workspaces under a paid subscription are locked, counted, and max is active_slots", async () => {
    await renderLoaded(
      afterTermEnd({
        active_count: 2,
        active_slots: 2,
        contracted_count: 1,
        selection_required: false,
        workspaces: [
          ws("w1", "Personal", { is_current: true }),
          ws("w2", "Research", {
            plan_name: "pro",
            under_billing_contract: true,
          }),
          ws("w3", "Side project", {
            status: "slot_suspended",
            suspended_at: "2026-12-01T00:00:12Z",
          }),
        ],
      }),
    );
    const research = screen.getByRole("checkbox", { name: /Research/ });
    expect(research).toHaveAttribute("aria-checked", "true");
    expect(research).toBeDisabled();
    const note = screen.getByText("keep.contracted");
    expect(research).toHaveAttribute("aria-describedby", note.id);
    expect(
      screen.getByText('keep.count|{"count":2,"max":2}'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: /Side project/ }),
    ).toBeDisabled();
    // Unchecking the uncontracted one frees the cap for the suspended one.
    pressSpace(screen.getByRole("checkbox", { name: /Personal/ }));
    expect(
      screen.getByRole("checkbox", { name: /Side project/ }),
    ).not.toBeDisabled();
    expect(research).toHaveAttribute("aria-checked", "true");
  });

  it("no slot can be freed: the preview names those ids, and a Save refused with SLOT-002 shows the remedies", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w1"],
        suspended: ["w2", "w3"],
        unmet: ["w3"],
        changes: { suspend: [], resume: [] },
      }),
    );
    mockSave.mockRejectedValue(
      apiError(409, "SLOT-002", {
        cap: 1,
        active_slots: 1,
        active_count: 1,
        contracted_count: 1,
        next_tier: "pro",
        cap_on_next_tier: 3,
        unmet: ["w3"],
      }),
    );
    await renderLoaded(afterTermEnd());
    pressSpace(screen.getByRole("checkbox", { name: /Personal/ }));
    pressSpace(screen.getByRole("checkbox", { name: /Side project/ }));
    await previewAndWait();
    expect(listItems("keep.unmet")).toEqual(["Side project"]);
    expect(listItems("keep.staysSuspended")).toEqual(["Research"]);
    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    expect(await screen.findByText("full.title")).toBeInTheDocument();
    expect(screen.getByText(/^full\.upgrade\|/)).toHaveTextContent('"cap":3');
    expect(mockToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ variant: "destructive" }),
    );
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
  it("explains the reason and offers resume, export and delete, each named after the workspace", async () => {
    await renderLoaded(afterTermEnd());
    const research = within(row("w2"));
    expect(research.getByText("reason")).toBeInTheDocument();
    expect(research.getByText(/status\.suspendedOn/)).toBeInTheDocument();
    for (const key of ["resumeNamed", "exportNamed", "deleteNamed"]) {
      expect(rowButton("w2", key, "Research")).toBeInTheDocument();
      expect(rowButton("w3", key, "Side project")).toBeInTheDocument();
    }
    expect(
      within(row("w1")).queryByRole("button", { name: /resumeNamed/ }),
    ).toBeNull();
  });

  it("an active workspace under a paid subscription says it cannot be deleted (#1940)", async () => {
    await renderLoaded(beforeTermEnd());
    expect(within(row("w2")).getByText("contractNote")).toBeInTheDocument();
    expect(within(row("w3")).queryByText("contractNote")).toBeNull();
  });

  it("export lists the contexts, each button named after its context, and returns focus to the opener", async () => {
    mockListContexts.mockResolvedValue({
      workspace_id: "w2",
      contexts: [
        { id: "c1", name: "papers", display_name: "Papers", is_private: false },
        { id: "c2", name: "My drafts", display_name: null, is_private: true },
      ],
    });
    await renderLoaded(afterTermEnd());
    const opener = rowButton("w2", "exportNamed", "Research");
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog");
    expect(mockListContexts).toHaveBeenCalledWith("w2");
    expect(
      await within(dialog).findByRole("button", {
        name: named("exportNamed", "Papers"),
      }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", {
        name: named("exportNamed", "My drafts"),
      }),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("export.private")).toBeInTheDocument();
    expect(within(dialog).getByText("export.privateNote")).toBeInTheDocument();

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("export of a workspace without contexts is an empty state", async () => {
    mockListContexts.mockResolvedValue({ workspace_id: "w3", contexts: [] });
    await renderLoaded(afterTermEnd());
    fireEvent.click(rowButton("w3", "exportNamed", "Side project"));
    expect(await screen.findByText("export.emptyTitle")).toBeInTheDocument();
  });

  it("delete asks first, deletes, refreshes quietly and puts focus on the table heading", async () => {
    mockDeleteWorkspace.mockResolvedValue(undefined);
    await renderLoaded(afterTermEnd());
    const after = afterTermEnd();
    mockGetSlots.mockResolvedValue({
      ...after,
      version: 5,
      owned_count: 2,
      workspaces: after.workspaces.filter((w) => w.id !== "w3"),
    });
    fireEvent.click(rowButton("w3", "deleteNamed", "Side project"));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("deleteConfirmTitle")).toBeInTheDocument();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "deleteWorkspace" }),
    );
    await waitFor(() => expect(mockDeleteWorkspace).toHaveBeenCalledWith("w3"));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(mockGetSlots).toHaveBeenCalledTimes(2);
    expect(mockRefreshWorkspaces).toHaveBeenCalledWith({ quiet: true });
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "table.caption" }),
      ).toHaveFocus(),
    );
  });

  it("a running paid subscription (409 BILLING-005) is said inside the delete dialog", async () => {
    mockDeleteWorkspace.mockRejectedValue(
      apiError(409, "BILLING-005", { workspace_ids: ["w3"] }),
    );
    await renderLoaded(afterTermEnd());
    fireEvent.click(rowButton("w3", "deleteNamed", "Side project"));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "deleteWorkspace" }),
    );
    expect(
      await within(dialog).findByText("deleteBlockedBySubscription"),
    ).toBeInTheDocument();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("resume with a free slot says so (politely announced) and saves the workspace first", async () => {
    const data = afterTermEnd({ active_slots: 2, active_count: 1 });
    mockPreview.mockResolvedValue(
      previewOf({
        cap: 2,
        active_slots: 2,
        active: ["w2", "w1"],
        suspended: ["w3"],
        changes: { suspend: [], resume: ["w2"] },
      }),
    );
    mockSave.mockResolvedValue(afterTermEnd({ version: 5 }));
    await renderLoaded(data);
    fireEvent.click(rowButton("w2", "resumeNamed", "Research"));
    const dialog = await screen.findByRole("alertdialog");
    const text = await within(dialog).findByText(
      'resume.freeSlot|{"name":"Research"}',
    );
    expect(text.closest("[aria-live]")).toHaveAttribute("aria-live", "polite");
    expect(mockPreview).toHaveBeenCalledWith(["w2", "w1"], "current");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "resume.confirm" }),
    );
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith(["w2", "w1"], 4));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("resume confirms what it previewed: a refetch while the dialog is open does not change the order or version", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w2"],
        suspended: ["w1", "w3"],
        changes: { suspend: ["w1"], resume: ["w2"] },
      }),
    );
    mockSave.mockRejectedValue(
      apiError(409, "SLOT-003", { expected_version: 4, current_version: 9 }),
    );
    await renderLoaded(afterTermEnd());
    fireEvent.click(rowButton("w2", "resumeNamed", "Research"));
    const dialog = await screen.findByRole("alertdialog");
    await within(dialog).findByText('resume.swap|{"name":"Research"}');

    // Meanwhile another tab swapped Side project in.
    mockGetSlots.mockResolvedValue(
      afterTermEnd({
        version: 9,
        workspaces: [
          ws("w1", "Personal", {
            status: "slot_suspended",
            suspended_at: "2026-12-02T00:00:00Z",
          }),
          ws("w2", "Research", {
            status: "slot_suspended",
            suspended_at: "2026-12-01T00:00:12Z",
          }),
          ws("w3", "Side project"),
        ],
      }),
    );
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockGetSlots).toHaveBeenCalledTimes(2));

    fireEvent.click(
      within(dialog).getByRole("button", { name: "resume.confirm" }),
    );
    await waitFor(() => expect(mockSave).toHaveBeenCalledWith(["w2", "w1"], 4));
    expect(await within(dialog).findByText("stale")).toBeInTheDocument();
    expect(mockPreview).toHaveBeenCalledTimes(1);
  });

  it("resume as a swap names the workspace that is suspended instead", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w2"],
        suspended: ["w1", "w3"],
        changes: { suspend: ["w1"], resume: ["w2"] },
      }),
    );
    await renderLoaded(afterTermEnd());
    fireEvent.click(rowButton("w2", "resumeNamed", "Research"));
    const dialog = await screen.findByRole("alertdialog");
    expect(
      await within(dialog).findByText('resume.swap|{"name":"Research"}'),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("listitem")).toHaveTextContent("Personal");
  });

  it("no slot can be freed (SLOT-002 on resume): the remedies name the next plan and deleting", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w2"],
        suspended: ["w1", "w3"],
        changes: { suspend: ["w1"], resume: ["w2"] },
      }),
    );
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
    fireEvent.click(rowButton("w2", "resumeNamed", "Research"));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(
      await within(dialog).findByRole("button", { name: "resume.confirm" }),
    );
    expect(await within(dialog).findByText("full.title")).toBeInTheDocument();
    expect(within(dialog).getByText(/^full\.upgrade\|/)).toHaveTextContent(
      '"cap":3',
    );
    expect(within(dialog).getByText("full.delete")).toBeInTheDocument();
  });

  it("no slot can be freed on the top plan: no upgrade sentence", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w1"],
        suspended: ["w2", "w3"],
        unmet: ["w2"],
      }),
    );
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
    fireEvent.click(rowButton("w2", "resumeNamed", "Research"));
    const dialog = await screen.findByRole("alertdialog");
    expect(await within(dialog).findByText("full.title")).toBeInTheDocument();
    expect(within(dialog).queryByText(/^full\.upgrade/)).toBeNull();
    expect(within(dialog).getByText("full.delete")).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "resume.confirm" }),
    ).toBeNull();
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
    expect(screen.queryByRole("button", { name: /resumeNamed/ })).toBeNull();
    expect(screen.queryByText("selectionRequired.title")).toBeNull();
    // The switcher and the guard read the workspace list: reloaded quietly.
    expect(mockRefreshWorkspaces).toHaveBeenCalledTimes(1);
    expect(mockRefreshWorkspaces).toHaveBeenCalledWith({ quiet: true });
  });

  it("a refetch that changes nothing does not reload the workspace list", async () => {
    await renderLoaded(afterTermEnd());
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await waitFor(() => expect(mockGetSlots).toHaveBeenCalledTimes(2));
    expect(mockRefreshWorkspaces).not.toHaveBeenCalled();
  });
});

describe("another change in progress (409 RES-002 workspace_slots_busy)", () => {
  const busy = () =>
    apiError(409, "RES-002", { reason: "workspace_slots_busy" });

  it("Save says try again in a moment, keeps the selection and the preview, and stays enabled", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w2"],
        suspended: ["w1", "w3"],
        changes: { suspend: ["w1"], resume: ["w2"] },
      }),
    );
    mockSave.mockRejectedValueOnce(busy());
    await renderLoaded(afterTermEnd());
    pressSpace(screen.getByRole("checkbox", { name: /Personal/ }));
    pressSpace(screen.getByRole("checkbox", { name: /Research/ }));
    await previewAndWait();
    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    expect(await screen.findByText("busy")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Research/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(
      screen.getByRole("region", { name: "keep.previewTitle" }),
    ).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "keep.save" });
    expect(save).not.toBeDisabled();
    expect(mockToast).not.toHaveBeenCalled();

    // The retry goes through.
    mockSave.mockResolvedValueOnce(afterTermEnd({ version: 5 }));
    fireEvent.click(save);
    await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(2));
    expect(mockSave).toHaveBeenLastCalledWith(["w2"], 4);
  });

  it("any other 409 RES-002 keeps the generic failure", async () => {
    mockPreview.mockResolvedValue(previewOf({ active: ["w1"], suspended: ["w2", "w3"] }));
    mockSave.mockRejectedValueOnce(apiError(409, "RES-002", { reason: "other" }));
    await renderLoaded(afterTermEnd());
    await previewAndWait();
    fireEvent.click(screen.getByRole("button", { name: "keep.save" }));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "destructive", title: "keep.saveFailed" }),
      ),
    );
    expect(screen.queryByText("busy")).toBeNull();
  });

  it("Resume says try again in a moment inside the dialog and keeps Resume enabled", async () => {
    mockPreview.mockResolvedValue(
      previewOf({
        active: ["w2"],
        suspended: ["w1", "w3"],
        changes: { suspend: ["w1"], resume: ["w2"] },
      }),
    );
    mockSave.mockRejectedValueOnce(busy());
    await renderLoaded(afterTermEnd());
    fireEvent.click(rowButton("w2", "resumeNamed", "Research"));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(
      await within(dialog).findByRole("button", { name: "resume.confirm" }),
    );
    expect(await within(dialog).findByText("busy")).toBeInTheDocument();
    expect(within(dialog).getByText('resume.swap|{"name":"Research"}')).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "resume.confirm" }),
    ).not.toBeDisabled();
  });
});


describe("#3010 integration services in force, next to the plan", () => {
  const until = (name: string, iso: string) =>
    `services.until|${JSON.stringify({ name, date: formatDate(iso, "UTC", "en") })}`;
  const servicesOf = (id: string, name: string) =>
    within(row(id)).queryByRole("list", {
      name: `services.label|${JSON.stringify({ name })}`,
    });

  it("one service: its name and end date under the plan", async () => {
    const data = beforeTermEnd();
    data.workspaces[1] = {
      ...data.workspaces[1],
      services: [
        { name: "Example Service", valid_until: "2026-12-14T00:00:00Z" },
      ],
    };
    await renderLoaded(data);
    const list = servicesOf("w2", "Research");
    expect(list).not.toBeNull();
    expect(
      within(list as HTMLElement)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([until("Example Service", "2026-12-14T00:00:00Z")]);
  });

  it("two services: one line each, in the order given, as plain text", async () => {
    const data = beforeTermEnd();
    data.workspaces[1] = {
      ...data.workspaces[1],
      services: [
        { name: "<b>Example Service</b>", valid_until: "2026-12-14T00:00:00Z" },
        { name: "other-sync", valid_until: "2027-01-01T00:00:05Z" },
      ],
    };
    await renderLoaded(data);
    const list = servicesOf("w2", "Research") as HTMLElement;
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual([
      until("<b>Example Service</b>", "2026-12-14T00:00:00Z"),
      until("other-sync", "2027-01-01T00:00:05Z"),
    ]);
    expect(list.querySelector("b")).toBeNull();
  });

  it("no services: no line (an empty list and a missing field alike)", async () => {
    const data = beforeTermEnd();
    data.workspaces[0] = { ...data.workspaces[0], services: [] };
    await renderLoaded(data);
    expect(servicesOf("w1", "Personal")).toBeNull();
    expect(servicesOf("w2", "Research")).toBeNull();
    expect(screen.queryByText(/^services\.until/)).toBeNull();
  });
});
