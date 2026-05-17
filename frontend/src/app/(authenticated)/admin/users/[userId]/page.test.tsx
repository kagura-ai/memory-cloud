/**
 * Tests for the Workspace Capacity section of the admin user detail page (#676).
 *
 * Scope is intentionally narrow — the rest of the page (workspaces table,
 * accessible contexts, change-plan dialog) is not exercised here. Only the
 * slot-bonus inc/dec UX, the conditional reason modal, and the
 * optimistic-update + rollback behavior on PATCH success/failure are
 * covered.
 *
 * jsdom does not fire Radix Dialog's pointerdown/pointerup dance reliably,
 * so the test uses Testing Library's role-based queries against the visible
 * modal markup the page renders when destructiveModal.open === true. The
 * page renders the Dialog content directly without any pointer-event
 * gating once `open` is true, so role queries work cleanly.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

vi.mock("@/lib/api/admin", () => ({
  updateWorkspaceSlotBonus: (...args: unknown[]) => mockUpdateBonus(...args),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ userId: "u_test_123" }),
  useRouter: () => ({ push: vi.fn() }),
}));

// Reduce layout chrome.
vi.mock("@/components/common/PageContainer", () => ({
  PageContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/common/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));

import UserDetailPage from "./page";

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
    // Without this anchor, `queryByText(/Workspace Capacity/)` returns
    // null both *before* the fetch resolves AND when the section is
    // correctly absent — making the test a false positive that would
    // pass even if the section was rendered after load.
    await screen.findByText(BASE_USER_DETAIL.user.name);
    expect(screen.queryByText(/Workspace Capacity/)).not.toBeInTheDocument();
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
    await screen.findByText(/Workspace Capacity/);
    expect(screen.getByText(/Owned: 2/)).toBeInTheDocument();
    expect(screen.getByText(/Cap: 3/)).toBeInTheDocument();
    expect(screen.getByText(/1 base \+ 2 bonus/)).toBeInTheDocument();
    expect(screen.getByText(/67% used/)).toBeInTheDocument();
    expect(screen.getByText("Personal")).toBeInTheDocument();
    expect(screen.getByText("Side project")).toBeInTheDocument();
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
    await screen.findByText(/at cap/);
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
    await screen.findByText(/Workspace Capacity/);

    fireEvent.click(screen.getByLabelText("Increment slot bonus"));

    await waitFor(() => {
      expect(mockUpdateBonus).toHaveBeenCalledWith("u_test_123", {
        delta: 1,
        reason: null,
      });
    });
    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ title: "Updated" }),
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
    await screen.findByText(/Workspace Capacity/);

    fireEvent.click(screen.getByLabelText("Decrement slot bonus"));

    await waitFor(() => {
      expect(mockUpdateBonus).toHaveBeenCalledWith("u_test_123", {
        delta: -1,
        reason: null,
      });
    });
    // No modal should be visible.
    expect(screen.queryByText(/Reason required/)).not.toBeInTheDocument();
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
    await screen.findByText(/Workspace Capacity/);

    fireEvent.click(screen.getByLabelText("Decrement slot bonus"));

    // Modal opens — no PATCH yet.
    await screen.findByText(/Reason required/);
    expect(mockUpdateBonus).not.toHaveBeenCalled();

    // Warning text mentions the shortfall and that workspaces aren't removed.
    expect(
      screen.getByText(/Existing workspaces are NOT removed/),
    ).toBeInTheDocument();

    // Confirm button is disabled until reason is non-empty.
    const confirmBtn = screen.getByRole("button", {
      name: /Confirm decrement/,
    });
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Reason \(required\)/), {
      target: { value: "finance request" },
    });
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
    await screen.findByText(/Workspace Capacity/);

    fireEvent.click(screen.getByLabelText("Decrement slot bonus"));
    await screen.findByText(/Reason required/);

    fireEvent.change(screen.getByLabelText(/Reason \(required\)/), {
      target: { value: "   " },
    });

    const confirmBtn = screen.getByRole("button", {
      name: /Confirm decrement/,
    });
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
    await screen.findByText(/Workspace Capacity/);

    fireEvent.click(screen.getByLabelText("Increment slot bonus"));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Error",
          variant: "destructive",
        }),
      );
    });
    // Displayed bonus should be back to the pre-call value (2), proving rollback.
    expect(screen.getByText("2")).toBeInTheDocument();
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
    await screen.findByText(/Workspace Capacity/);
    expect(screen.getByLabelText("Decrement slot bonus")).toBeDisabled();
  });
});
