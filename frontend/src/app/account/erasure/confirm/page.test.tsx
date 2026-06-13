/**
 * Tests for the OAuth erasure-confirmation landing page (Issue #953).
 *
 * Covers the four branches of the gating effect: missing token, unauthenticated
 * (bounce to /login?return_to with the one-shot loop guard), authenticated
 * success, and authenticated failure (expired/used token).
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// i18n: stub returns the key (surfacing an interpolated {date}).
vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (key: string, values?: Record<string, unknown>) =>
    values && "date" in values ? `${key}|${values.date}` : key,
  useLocale: () => "en",
}));

// Mutable auth + token, flipped per test.
let mockAuth: { user: unknown; isLoading: boolean; isAuthenticated: boolean } = {
  user: { timezone: "UTC" },
  isLoading: false,
  isAuthenticated: true,
};
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockAuth,
}));

let mockToken: string | null = "tok-1";
const mockReplace = vi.fn();
const mockPush = vi.fn();
vi.mock("next/navigation", () => {
  // Return a STABLE router object across calls — the real next/navigation
  // useRouter is referentially stable, and the confirm page's effect lists
  // `router` in its deps. A fresh object per render would re-run the effect
  // and spuriously defeat the one-shot loop guard.
  let router: { replace: typeof mockReplace; push: typeof mockPush } | null = null;
  return {
    useRouter: () => (router ??= { replace: mockReplace, push: mockPush }),
    useSearchParams: () => ({ get: (k: string) => (k === "token" ? mockToken : null) }),
  };
});

const mockConfirm = vi.fn();
vi.mock("@/lib/api/account-erasure", () => ({
  confirmErasure: (...a: unknown[]) => mockConfirm(...a),
}));

import ConfirmErasurePage from "./page";

function confirmedState() {
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
  sessionStorage.clear();
  mockReplace.mockReset();
  mockPush.mockReset();
  mockConfirm.mockReset();
  mockAuth = { user: { timezone: "UTC" }, isLoading: false, isAuthenticated: true };
  mockToken = "tok-1";
});

describe("ConfirmErasurePage", () => {
  it("shows the invalid state when no token is present", async () => {
    mockToken = null;
    render(<ConfirmErasurePage />);
    expect(await screen.findByText("confirmPageInvalidBody")).toBeInTheDocument();
    expect(mockConfirm).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("bounces an unauthenticated user to /login preserving the token in return_to", async () => {
    mockAuth = { user: null, isLoading: false, isAuthenticated: false };
    render(<ConfirmErasurePage />);
    await waitFor(() => expect(mockReplace).toHaveBeenCalledTimes(1));
    const target = mockReplace.mock.calls[0][0] as string;
    expect(target).toContain("/login?return_to=");
    expect(target).toContain(encodeURIComponent("/account/erasure/confirm?token=tok-1"));
    expect(sessionStorage.getItem("erasure_confirm_redirected")).toBe("1");
    expect(mockConfirm).not.toHaveBeenCalled();
  });

  it("does NOT redirect again if it already bounced (loop guard) — shows invalid", async () => {
    sessionStorage.setItem("erasure_confirm_redirected", "1");
    mockAuth = { user: null, isLoading: false, isAuthenticated: false };
    render(<ConfirmErasurePage />);
    expect(await screen.findByText("confirmPageInvalidBody")).toBeInTheDocument();
    expect(mockReplace).not.toHaveBeenCalled();
    expect(sessionStorage.getItem("erasure_confirm_redirected")).toBeNull();
  });

  it("confirms with the token (no password) and shows success when authenticated", async () => {
    mockConfirm.mockResolvedValue(confirmedState());
    render(<ConfirmErasurePage />);
    await waitFor(() => expect(mockConfirm).toHaveBeenCalledWith("tok-1"));
    expect(await screen.findByText(/^confirmPageSuccessBody\|/)).toBeInTheDocument();
  });

  it("shows the invalid state when confirm fails (expired/used token)", async () => {
    mockConfirm.mockRejectedValue(new Error("400"));
    render(<ConfirmErasurePage />);
    await waitFor(() => expect(mockConfirm).toHaveBeenCalledWith("tok-1"));
    expect(await screen.findByText("confirmPageInvalidBody")).toBeInTheDocument();
  });
});
