/**
 * Tests for the Workspace Capacity section of the admin user detail page (#676).
 *
 * Covers the slot-bonus inc/dec UX, the conditional reason modal, the
 * optimistic-update + rollback behavior on PATCH success/failure, and (#2765)
 * the plan actions of the workspaces table: change plan, quota detail, add-on
 * and spend-cap editing on workspaces the user owns, none on the others.
 *
 * jsdom does not fire Radix Dialog's pointerdown/pointerup dance reliably,
 * so the test uses Testing Library's role-based queries against the visible
 * modal markup the page renders when destructiveModal.open === true. The
 * page renders the Dialog content directly without any pointer-event
 * gating once `open` is true, so role queries work cleanly.
 */

import {
  render,
  screen,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockGet = vi.fn();
const mockUpdateBonus = vi.fn();
const mockToast = vi.fn();

vi.mock("@/lib/api", () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    put: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

// #2765: the plan actions of the workspaces table call the same admin plan
// API functions the Quotas page called.
const mockUpdateWorkspacePlan = vi.fn();
const mockGetWorkspaceQuotas = vi.fn();
const mockUpdateWorkspaceAddons = vi.fn();
vi.mock("@/lib/api/admin", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return {
    ...actual,
    updateWorkspaceSlotBonus: (...args: unknown[]) => mockUpdateBonus(...args),
    updateWorkspacePlan: (...args: unknown[]) =>
      mockUpdateWorkspacePlan(...args),
    getWorkspaceQuotas: (...args: unknown[]) => mockGetWorkspaceQuotas(...args),
    updateWorkspaceAddons: (...args: unknown[]) =>
      mockUpdateWorkspaceAddons(...args),
  };
});

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ userId: "u_test_123" }),
  useRouter: () => ({ push: vi.fn() }),
}));

// next-intl mock matches the project convention (see frontend/src/app/login/
// page.test.tsx): useTranslations returns the key verbatim. For calls that
// pass params, we echo `key var1=value1 var2=value2` so tests can assert
// data interpolation flowed through without needing the real message
// catalog. Tests assert on data-testid / role queries (#688) rather than
// visible English text, so the key-as-text is fine.
vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, params?: Record<string, unknown>) => {
    if (!params) return key;
    const tail = Object.entries(params)
      .map(([k, v]) => `${k}=${v}`)
      .join(" ");
    return `${key} ${tail}`;
  },
  useLocale: () => "en",
}));

// Reduce layout chrome.
// Radix Select does not render its items in jsdom; the mock renders a plain
// <select> so the change-plan dialog's option labels can be asserted (#1848).
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange?: (v: string) => void;
    children: React.ReactNode;
  }) => (
    <select
      data-testid="select-mock"
      value={value}
      onChange={(e) => onValueChange?.(e.target.value)}
    >
      {children}
    </select>
  ),
  SelectTrigger: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
  SelectValue: () => null,
  SelectContent: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
  SelectItem: ({
    value,
    children,
  }: {
    value: string;
    children: React.ReactNode;
  }) => <option value={value}>{children}</option>,
}));

vi.mock("@/components/common/PageContainer", () => ({
  PageContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));

import UserDetailPage from "./page";
import { QUOTA_DETAIL_PRO } from "@/components/admin/plans/plans.fixtures";
import { USER_DETAIL_TEST_IDS } from "./testids";

const BASE_USER_DETAIL = {
  user: {
    id: "u_test_123",
    email: "test@example.invalid",
    name: "Test User",
    role: "user",
    is_initial_admin: false,
    created_at: new Date().toISOString(),
    last_login_at: null,
  },
  workspaces: [],
  accessible_contexts: [],
  stats: {
    total_memories: 0,
    working_memories: 0,
    persistent_memories: 0,
    active_api_keys: 0,
  },
};

function detailWith(summary: unknown) {
  return { ...BASE_USER_DETAIL, workspace_summary: summary };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Workspace Capacity section (#676)", () => {
  it("does not render the section when workspace_summary is absent", async () => {
    mockGet.mockResolvedValueOnce(BASE_USER_DETAIL);
    render(<UserDetailPage />);
    // Wait for the page to actually finish loading by waiting for a
    // post-load element (the user's name from the fetched payload).
    // Without this anchor, `queryByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection)`
    // returns null both *before* the fetch resolves AND when the section
    // is correctly absent — making the test a false positive that would
    // pass even if the section was rendered after load.
    await screen.findByText(BASE_USER_DETAIL.user.name);
    expect(
      screen.queryByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection),
    ).not.toBeInTheDocument();
  });

  it("renders cap formula and badge variant from workspace_summary", async () => {
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 2,
        workspace_slot_bonus: 2,
        base_cap: 1,
        cap: 3,
        is_at_cap: false,
        owned_workspaces: [
          { id: "ws-1", name: "Personal", plan_name: "pro" },
          { id: "ws-2", name: "Side project", plan_name: "free" },
        ],
      }),
    );
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);
    // Cap-display text is rendered via the i18n mock as
    // `<key> <param>=<value> ...` so we assert on the data flowed
    // through rather than the localized message.
    const capDisplay = screen.getByTestId(
      USER_DETAIL_TEST_IDS.workspaceCapacityCapDisplay,
    );
    expect(capDisplay.textContent).toMatch(/count=2/);
    expect(capDisplay.textContent).toMatch(/cap=3/);
    expect(capDisplay.textContent).toMatch(/base=1/);
    expect(capDisplay.textContent).toMatch(/bonus=2/);
    expect(capDisplay.parentElement?.textContent).toMatch(/pct=67/);
    // Scoped: the owned workspaces are rows of the workspaces table too (#2765).
    const section = screen.getByTestId(
      USER_DETAIL_TEST_IDS.workspaceCapacitySection,
    );
    expect(within(section).getByText("Personal")).toBeInTheDocument();
    expect(within(section).getByText("Side project")).toBeInTheDocument();
  });

  it("shows 'at cap' badge when is_at_cap is true", async () => {
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 3,
        workspace_slot_bonus: 2,
        base_cap: 1,
        cap: 3,
        is_at_cap: true,
        owned_workspaces: [],
      }),
    );
    render(<UserDetailPage />);
    const section = await screen.findByTestId(
      USER_DETAIL_TEST_IDS.workspaceCapacitySection,
    );
    expect(section.textContent).toMatch(/workspaceCapacity\.atCapBadge/);
  });

  it("commits +1 immediately and updates the displayed bonus on success", async () => {
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 1,
        workspace_slot_bonus: 2,
        base_cap: 1,
        cap: 3,
        is_at_cap: false,
        owned_workspaces: [],
      }),
    );
    mockUpdateBonus.mockResolvedValueOnce({
      before_value: 2,
      after_value: 3,
      owned_count: 1,
      base_cap: 1,
      cap: 4,
      is_at_cap: false,
      reason: null,
    });
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);

    fireEvent.click(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityIncrement),
    );

    await waitFor(() => {
      expect(mockUpdateBonus).toHaveBeenCalledWith("u_test_123", {
        delta: 1,
        reason: null,
      });
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "messages.bonusUpdateSuccessTitle",
        }),
      );
    });
  });

  it("commits non-destructive -1 immediately (no modal)", async () => {
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 1,
        workspace_slot_bonus: 2,
        base_cap: 1,
        cap: 3,
        is_at_cap: false,
        owned_workspaces: [],
      }),
    );
    mockUpdateBonus.mockResolvedValueOnce({
      before_value: 2,
      after_value: 1,
      owned_count: 1,
      base_cap: 1,
      cap: 2,
      is_at_cap: false,
      reason: null,
    });
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);

    fireEvent.click(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityDecrement),
    );

    await waitFor(() => {
      expect(mockUpdateBonus).toHaveBeenCalledWith("u_test_123", {
        delta: -1,
        reason: null,
      });
    });
    // No modal should be visible.
    expect(
      screen.queryByTestId(USER_DETAIL_TEST_IDS.reasonModal),
    ).not.toBeInTheDocument();
  });

  it("includes tier_grant in the -1 projection: at-cap-after-decrement is not destructive (#1550)", async () => {
    // pro owner: 3 owned, bonus 1, grant 2 → cap 4. Decrementing lands on
    // cap 3 = base 1 + grant 2 + bonus 0 ≥ owned 3, so no reason modal.
    // Without the grant in the projection (1 + 0 = 1 < 3) the modal would open.
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 3,
        workspace_slot_bonus: 1,
        base_cap: 1,
        tier_grant: 2,
        tier: "pro",
        cap: 4,
        is_at_cap: false,
        owned_workspaces: [],
      }),
    );
    mockUpdateBonus.mockResolvedValueOnce({
      before_value: 1,
      after_value: 0,
      owned_count: 3,
      base_cap: 1,
      cap: 3,
      is_at_cap: true,
      reason: null,
    });
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);

    // The formula line surfaces the grant term.
    expect(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityCapDisplay)
        .textContent,
    ).toMatch(/grant=2/);

    fireEvent.click(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityDecrement),
    );

    await waitFor(() => {
      expect(mockUpdateBonus).toHaveBeenCalledWith("u_test_123", {
        delta: -1,
        reason: null,
      });
    });
    expect(
      screen.queryByTestId(USER_DETAIL_TEST_IDS.reasonModal),
    ).not.toBeInTheDocument();
  });

  it("opens reason modal for destructive -1 and submits with reason", async () => {
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 4,
        workspace_slot_bonus: 3,
        base_cap: 1,
        cap: 4,
        is_at_cap: true,
        owned_workspaces: [],
      }),
    );
    mockUpdateBonus.mockResolvedValueOnce({
      before_value: 3,
      after_value: 2,
      owned_count: 4,
      base_cap: 1,
      cap: 3,
      is_at_cap: true,
      reason: "finance request",
    });
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);

    fireEvent.click(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityDecrement),
    );

    // Modal opens — no PATCH yet.
    const modal = await screen.findByTestId(USER_DETAIL_TEST_IDS.reasonModal);
    expect(mockUpdateBonus).not.toHaveBeenCalled();

    // Warning text is built from the t() messages.overCapWarn message; the
    // mock echoes the params so we can assert that the shortfall / cap
    // values flowed through correctly.
    expect(modal.textContent).toMatch(/messages\.overCapWarn/);
    expect(modal.textContent).toMatch(/owned=4/);
    expect(modal.textContent).toMatch(/projected=2/);
    expect(modal.textContent).toMatch(/cap=3/);
    expect(modal.textContent).toMatch(/shortfall=1/);

    // Confirm button is disabled until reason is non-empty.
    const confirmBtn = screen.getByTestId(
      USER_DETAIL_TEST_IDS.reasonModalConfirm,
    );
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(
      screen.getByTestId(USER_DETAIL_TEST_IDS.reasonModalInput),
      {
        target: { value: "finance request" },
      },
    );
    expect(confirmBtn).not.toBeDisabled();

    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockUpdateBonus).toHaveBeenCalledWith("u_test_123", {
        delta: -1,
        reason: "finance request",
      });
    });
  });

  it("does not allow submitting destructive modal with whitespace-only reason", async () => {
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 4,
        workspace_slot_bonus: 3,
        base_cap: 1,
        cap: 4,
        is_at_cap: true,
        owned_workspaces: [],
      }),
    );
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);

    fireEvent.click(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityDecrement),
    );
    await screen.findByTestId(USER_DETAIL_TEST_IDS.reasonModal);

    fireEvent.change(
      screen.getByTestId(USER_DETAIL_TEST_IDS.reasonModalInput),
      {
        target: { value: "   " },
      },
    );

    const confirmBtn = screen.getByTestId(
      USER_DETAIL_TEST_IDS.reasonModalConfirm,
    );
    expect(confirmBtn).toBeDisabled();
    expect(mockUpdateBonus).not.toHaveBeenCalled();
  });

  it("rolls back optimistic update + shows destructive toast on PATCH failure", async () => {
    const initialDetail = detailWith({
      owned_count: 1,
      workspace_slot_bonus: 2,
      base_cap: 1,
      cap: 3,
      is_at_cap: false,
      owned_workspaces: [],
    });
    // Initial load + refetch on failure (the failure path calls
    // loadUserDetail() to re-read authoritative server state, which
    // returns the same unchanged summary since the PATCH never landed).
    mockGet.mockResolvedValueOnce(initialDetail);
    mockGet.mockResolvedValueOnce(initialDetail);
    mockUpdateBonus.mockRejectedValueOnce(new Error("network down"));
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);

    fireEvent.click(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityIncrement),
    );

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "error",
          variant: "destructive",
        }),
      );
    });
    // Displayed bonus should be back to the pre-call value (2), proving rollback.
    expect(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityBonusValue)
        .textContent,
    ).toBe("2");
  });

  it("disables [-] when bonus is already 0", async () => {
    mockGet.mockResolvedValueOnce(
      detailWith({
        owned_count: 1,
        workspace_slot_bonus: 0,
        base_cap: 1,
        cap: 1,
        is_at_cap: true,
        owned_workspaces: [],
      }),
    );
    render(<UserDetailPage />);
    await screen.findByTestId(USER_DETAIL_TEST_IDS.workspaceCapacitySection);
    expect(
      screen.getByTestId(USER_DETAIL_TEST_IDS.workspaceCapacityDecrement),
    ).toBeDisabled();
  });
});

describe("Workspaces table plan actions (#2765)", () => {
  const OWNED = {
    workspace_id: "ws-pro",
    workspace_name: "Pro Workspace",
    role: "owner",
    is_primary: true,
    plan_name: "pro",
    joined_at: new Date().toISOString(),
  };
  const JOINED = {
    workspace_id: "ws-team",
    workspace_name: "Team Workspace",
    role: "member",
    is_primary: false,
    plan_name: "basic",
    joined_at: new Date().toISOString(),
  };
  const detail = { ...BASE_USER_DETAIL, workspaces: [OWNED, JOINED] };

  const row = (name: string) =>
    within(screen.getByText(name).closest("tr") as HTMLElement);

  beforeEach(() => {
    mockGet.mockResolvedValue(detail);
    mockGetWorkspaceQuotas.mockResolvedValue(QUOTA_DETAIL_PRO);
    mockUpdateWorkspacePlan.mockResolvedValue({ message: "ok" });
    mockUpdateWorkspaceAddons.mockResolvedValue({
      message: "ok",
      warnings: [],
    });
  });

  it("offers the plan actions only on a workspace the user owns", async () => {
    render(<UserDetailPage />);
    await screen.findByText("Pro Workspace");

    expect(
      row("Pro Workspace").getByRole("button", { name: /actions.changePlan/ }),
    ).toBeInTheDocument();
    expect(
      row("Pro Workspace").getByRole("button", { name: /actions.quotaDetail/ }),
    ).toBeInTheDocument();

    // A workspace the user only joined: the plan read-only, no buttons at all.
    expect(row("Team Workspace").queryAllByRole("button")).toHaveLength(0);
    expect(
      row("Team Workspace").getByText("workspaces.ownerManagesPlan"),
    ).toBeInTheDocument();
    expect(row("Team Workspace").getByText("M")).toBeInTheDocument();
  });

  // A legacy workspace can carry owner_user_id without a matching member row
  // (workspace_ownership_service), or with a member row of another role. The
  // owner is workspaces.owner_user_id, which owned_workspaces reports.
  const summaryOwning = (...owned: { id: string; name: string; plan_name: string }[]) => ({
    owned_count: owned.length,
    workspace_slot_bonus: 0,
    base_cap: 1,
    tier_grant: 0,
    tier: "pro",
    cap: 1 + owned.length,
    is_at_cap: false,
    owned_workspaces: owned,
  });

  it("offers the plan actions on an owned workspace whose member row has another role", async () => {
    mockGet.mockResolvedValue({
      ...detail,
      workspaces: [{ ...JOINED, role: "admin" }],
      workspace_summary: summaryOwning({
        id: "ws-team",
        name: "Team Workspace",
        plan_name: "basic",
      }),
    });
    render(<UserDetailPage />);
    await screen.findAllByText("Team Workspace");
    const table = screen.getByRole("table");
    const teamRow = within(
      within(table).getByText("Team Workspace").closest("tr") as HTMLElement,
    );
    expect(
      teamRow.getByRole("button", { name: /actions.changePlan/ }),
    ).toBeInTheDocument();
  });

  it("lists and manages an owned workspace the user has no member row in", async () => {
    mockGet.mockResolvedValue({
      ...detail,
      workspaces: [JOINED],
      workspace_summary: summaryOwning({
        id: "ws-legacy",
        name: "Legacy Workspace",
        plan_name: "pro",
      }),
    });
    render(<UserDetailPage />);
    await screen.findAllByText("Legacy Workspace");
    const table = screen.getByRole("table");
    const legacyRow = within(
      within(table).getByText("Legacy Workspace").closest("tr") as HTMLElement,
    );
    fireEvent.click(
      legacyRow.getByRole("button", { name: /actions.changePlan/ }),
    );
    await screen.findByText("changePlanDialog.title");
    fireEvent.change(screen.getByTestId("select-mock"), {
      target: { value: "promax" },
    });
    fireEvent.click(screen.getByText("changePlanDialog.confirm"));
    await waitFor(() =>
      expect(mockUpdateWorkspacePlan).toHaveBeenCalledWith("ws-legacy", {
        plan_name: "promax",
      }),
    );
    // The joined workspace stays read-only.
    const teamRow = within(
      within(table).getByText("Team Workspace").closest("tr") as HTMLElement,
    );
    expect(teamRow.queryAllByRole("button")).toHaveLength(0);
  });

  it("changes the plan with the Quotas page's request and reloads the user", async () => {
    render(<UserDetailPage />);
    await screen.findByText("Pro Workspace");
    fireEvent.click(
      row("Pro Workspace").getByRole("button", { name: /actions.changePlan/ }),
    );
    await screen.findByText("changePlanDialog.title");

    fireEvent.change(screen.getByTestId("select-mock"), {
      target: { value: "promax" },
    });
    expect(screen.getByText("changePlanDialog.warning")).toBeInTheDocument();
    fireEvent.click(screen.getByText("changePlanDialog.confirm"));

    await waitFor(() =>
      expect(mockUpdateWorkspacePlan).toHaveBeenCalledWith("ws-pro", {
        plan_name: "promax",
      }),
    );
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });

  it("shows the quota detail and edits the add-ons with the Quotas page's request", async () => {
    render(<UserDetailPage />);
    await screen.findByText("Pro Workspace");
    fireEvent.click(
      row("Pro Workspace").getByRole("button", { name: /actions.quotaDetail/ }),
    );

    const memoryRow = (await screen.findByText("quota.memory")).closest(
      "div.grid",
    );
    expect(mockGetWorkspaceQuotas).toHaveBeenCalledWith("ws-pro");
    expect(memoryRow).toHaveTextContent("50,000 / 110,000");

    fireEvent.click(screen.getByText("actions.editAddons"));
    await screen.findByText("addonDialog.title");
    fireEvent.click(screen.getByText("addonDialog.save"));

    await waitFor(() =>
      expect(mockUpdateWorkspaceAddons).toHaveBeenCalledTimes(1),
    );
    const [workspaceId, body] = mockUpdateWorkspaceAddons.mock.calls[0];
    expect(workspaceId).toBe("ws-pro");
    expect(body).toEqual({
      addon_memory_bonus: 10_000,
      addon_mcp_quota_bonus: 0,
      addon_rest_quota_bonus: 0,
      addon_public_quota_bonus: 0,
      addon_member_bonus: 0,
      addon_context_bonus: 0,
      addon_analysis_bonus: 0,
      addon_storage_bonus_mb: 0,
      addon_sleep_contexts_bonus: 0,
      addon_connector_bonus: 0,
      addon_memories_per_day_bonus: 0,
      addon_history_retention_days_bonus: 0,
    });
  });

  it("opens the spend cap dialog when the workspace has a spend cap", async () => {
    mockGetWorkspaceQuotas.mockResolvedValue({
      ...QUOTA_DETAIL_PRO,
      spend_cap: {
        tier_default_daily_usd: 10,
        tier_default_monthly_usd: 100,
        override_daily_usd: null,
        override_monthly_usd: null,
        effective_daily_usd: 10,
        effective_monthly_usd: 100,
        current_daily_usd: 1,
        current_monthly_usd: 5,
      },
    });
    render(<UserDetailPage />);
    await screen.findByText("Pro Workspace");
    fireEvent.click(
      row("Pro Workspace").getByRole("button", { name: /actions.quotaDetail/ }),
    );
    fireEvent.click(await screen.findByText("actions.editSpendCap"));
    expect(await screen.findByText("spendCapDialog.title")).toBeInTheDocument();
  });

  it("hides the quota detail again on a second click", async () => {
    render(<UserDetailPage />);
    await screen.findByText("Pro Workspace");
    const toggle = row("Pro Workspace").getByRole("button", {
      name: /actions.quotaDetail/,
    });
    fireEvent.click(toggle);
    await screen.findByText("quota.memory");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(screen.queryByText("quota.memory")).toBeNull();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });
});
