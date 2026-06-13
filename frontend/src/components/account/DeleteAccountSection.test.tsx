/**
 * Tests for the self-serve account deletion danger zone (Issue #953).
 *
 * Covers the three mount states and both confirm channels:
 *   - no active request → delete button
 *   - password-auth flow → password step → confirmErasure(token, password)
 *   - OAuth flow (confirm_token null) → "check your email" message
 *   - active cooling-off request → scheduled state + cancel
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// i18n: stub returns the key, surfacing an interpolated {date} so we can assert.
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (key: string, values?: Record<string, unknown>) =>
    values && "date" in values ? `${key}|${values.date}` : key,
  useLocale: () => "en",
}));

// Flipped per test (e.g. is_initial_admin) — same mutable-mock pattern as the
// profile page test.
let mockUser: { timezone?: string; is_initial_admin?: boolean } | null = {
  timezone: "UTC",
};
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockUser, isLoading: false, isAuthenticated: true }),
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

// Mock the network calls; keep erasureStage real so its logic is exercised.
const mockRequest = vi.fn();
const mockConfirm = vi.fn();
const mockCancel = vi.fn();
const mockGetActive = vi.fn();
vi.mock("@/lib/api/account-erasure", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api/account-erasure")>();
  return {
    ...actual,
    requestErasure: () => mockRequest(),
    confirmErasure: (...a: unknown[]) => mockConfirm(...a),
    cancelErasure: () => mockCancel(),
    getActiveErasureRequest: () => mockGetActive(),
  };
});

import { DeleteAccountSection } from "./DeleteAccountSection";
import type { ErasureRequestState } from "@/lib/api/account-erasure";
import { ApiError } from "@/lib/api/base";

function coolingOffState(): ErasureRequestState {
  return {
    request_id: "r1",
    status: "cooling_off",
    is_self_service: true,
    requested_at: "2026-06-13T00:00:00Z",
    confirmed_at: "2026-06-13T00:01:00Z",
    scheduled_for: "2026-06-20T00:01:00Z",
    started_at: null,
    completed_at: null,
    cancelled_at: null,
    failure_reason: null,
  };
}

beforeEach(() => {
  mockUser = { timezone: "UTC" };
  mockToast.mockReset();
  mockRequest.mockReset();
  mockConfirm.mockReset();
  mockCancel.mockReset();
  mockGetActive.mockReset();
});

describe("DeleteAccountSection", () => {
  it("shows the delete button when there is no active request", async () => {
    mockGetActive.mockResolvedValue(null);
    render(<DeleteAccountSection />);
    expect(await screen.findByText("deleteButton")).toBeInTheDocument();
  });

  it("hides the delete control and shows a protected note for the initial admin", async () => {
    mockUser = { timezone: "UTC", is_initial_admin: true };
    mockGetActive.mockResolvedValue(null);
    render(<DeleteAccountSection />);
    expect(await screen.findByText("protectedAccount")).toBeInTheDocument();
    expect(screen.queryByText("deleteButton")).not.toBeInTheDocument();
  });

  it("password flow: opens dialog → password step → confirms with token + password", async () => {
    mockGetActive.mockResolvedValue(null);
    mockRequest.mockResolvedValue({
      request_id: "r1",
      status: "pending",
      requested_at: "x",
      confirm_token: "tok-123",
    });
    mockConfirm.mockResolvedValue(coolingOffState());

    render(<DeleteAccountSection />);
    fireEvent.click(await screen.findByText("deleteButton"));

    // Intro step → Continue triggers the request.
    fireEvent.click(await screen.findByText("dialogConfirm"));

    // Password step appears; fill it and confirm.
    const pw = await screen.findByLabelText("passwordLabel");
    fireEvent.change(pw, { target: { value: "hunter2" } });
    fireEvent.click(screen.getByText("dialogConfirm"));

    await waitFor(() =>
      expect(mockConfirm).toHaveBeenCalledWith("tok-123", "hunter2"),
    );
    // ...and the success transition renders the cooling-off card (asserts the
    // setActive/setOpen effect, not just that confirm was called).
    expect(await screen.findByText(/^scheduledBody\|/)).toBeInTheDocument();
  });

  it("password step ERASURE-002: shows the expired-token message, not 'wrong password'", async () => {
    mockGetActive.mockResolvedValue(null);
    mockRequest.mockResolvedValue({
      request_id: "r1",
      status: "pending",
      requested_at: "x",
      confirm_token: "tok-123",
    });
    mockConfirm.mockRejectedValue(
      new ApiError({ status: 400, error: "ERASURE-002", message: "x" }),
    );
    render(<DeleteAccountSection />);
    fireEvent.click(await screen.findByText("deleteButton"));
    fireEvent.click(await screen.findByText("dialogConfirm"));
    const pw = await screen.findByLabelText("passwordLabel");
    fireEvent.change(pw, { target: { value: "hunter2" } });
    fireEvent.click(screen.getByText("dialogConfirm"));
    expect(await screen.findByText("confirmTokenExpired")).toBeInTheDocument();
    expect(screen.queryByText("confirmError")).not.toBeInTheDocument();
  });

  it("OAuth flow: a null confirm_token shows the check-your-email message", async () => {
    mockGetActive.mockResolvedValue(null);
    mockRequest.mockResolvedValue({
      request_id: "r1",
      status: "pending",
      requested_at: "x",
      confirm_token: null,
    });

    render(<DeleteAccountSection />);
    fireEvent.click(await screen.findByText("deleteButton"));
    fireEvent.click(await screen.findByText("dialogConfirm"));

    expect(await screen.findByText("emailSentBody")).toBeInTheDocument();
    expect(mockConfirm).not.toHaveBeenCalled();
  });

  it("cooling-off: shows the scheduled state and cancels on click", async () => {
    mockGetActive.mockResolvedValue(coolingOffState());
    mockCancel.mockResolvedValue({ ...coolingOffState(), status: "cancelled", cancelled_at: "x" });

    render(<DeleteAccountSection />);

    // Scheduled message (with interpolated date) replaces the delete button.
    expect(await screen.findByText(/^scheduledBody\|/)).toBeInTheDocument();
    expect(screen.queryByText("deleteButton")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("cancelButton"));
    await waitFor(() => expect(mockCancel).toHaveBeenCalled());
    await waitFor(() => expect(mockToast).toHaveBeenCalledWith({ title: "cancelSuccess" }));
  });

  it("pending: shows the awaiting-confirmation state with a cancel button (no delete button)", async () => {
    mockGetActive.mockResolvedValue({
      ...coolingOffState(),
      status: "pending",
      confirmed_at: null,
      scheduled_for: null,
    });
    render(<DeleteAccountSection />);
    expect(await screen.findByText("pendingBody")).toBeInTheDocument();
    expect(screen.getByText("cancelButton")).toBeInTheDocument();
    expect(screen.queryByText("deleteButton")).not.toBeInTheDocument();
  });

  it("in_progress: shows the executing state with NO cancel button", async () => {
    mockGetActive.mockResolvedValue({
      ...coolingOffState(),
      status: "in_progress",
      started_at: "2026-06-20T00:02:00Z",
    });
    render(<DeleteAccountSection />);
    expect(await screen.findByText("inProgressBody")).toBeInTheDocument();
    expect(screen.queryByText("cancelButton")).not.toBeInTheDocument();
    expect(screen.queryByText("deleteButton")).not.toBeInTheDocument();
  });

  it("ERASURE-005: shows the workspace-transfer message in the dialog", async () => {
    mockGetActive.mockResolvedValue(null);
    mockRequest.mockRejectedValue(
      new ApiError({ status: 409, error: "ERASURE-005", message: "x" }),
    );
    render(<DeleteAccountSection />);
    fireEvent.click(await screen.findByText("deleteButton"));
    fireEvent.click(await screen.findByText("dialogConfirm"));
    expect(await screen.findByText("workspaceTransferError")).toBeInTheDocument();
    expect(mockConfirm).not.toHaveBeenCalled();
  });

  it("ERASURE-006: re-syncs the existing request and toasts instead of looping", async () => {
    // mount → no request; after the 409, loadActive() returns the existing one.
    mockGetActive
      .mockResolvedValueOnce(null)
      .mockResolvedValueOnce(coolingOffState());
    mockRequest.mockRejectedValue(
      new ApiError({ status: 409, error: "ERASURE-006", message: "x" }),
    );
    render(<DeleteAccountSection />);
    fireEvent.click(await screen.findByText("deleteButton"));
    fireEvent.click(await screen.findByText("dialogConfirm"));
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith({ title: "alreadyRequested" }),
    );
    // Re-synced: the cooling-off card now renders (no "try again" loop).
    expect(await screen.findByText(/^scheduledBody\|/)).toBeInTheDocument();
  });
});
