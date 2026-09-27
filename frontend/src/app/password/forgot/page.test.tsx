/**
 * Forgot-password page (#1678): one neutral answer whatever the backend knows,
 * a toast only for a rate limit or a failure.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// Stable like the real useTranslations().
const mockT = (key: string) => key;
vi.mock("next-intl", () => ({ useTranslations: () => mockT }));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const { mockRequestReset, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number;
    constructor(status: number) {
      super("fake");
      this.status = status;
    }
  }
  return { mockRequestReset: vi.fn(), FakeApiError };
});
vi.mock("@/lib/auth/auth", () => ({
  requestPasswordReset: (...a: unknown[]) => mockRequestReset(...a),
}));
vi.mock("@/lib/api/base", () => ({ ApiError: FakeApiError }));

import ForgotPasswordPage from "./page";

function submit(email: string) {
  fireEvent.change(screen.getByLabelText("forgot.emailLabel"), {
    target: { value: email },
  });
  fireEvent.click(screen.getByRole("button", { name: "forgot.submit" }));
}

beforeEach(() => {
  mockRequestReset.mockReset();
  mockToast.mockReset();
});

describe("ForgotPasswordPage", () => {
  it("sends the trimmed email and shows the neutral message", async () => {
    mockRequestReset.mockResolvedValue(undefined);
    render(<ForgotPasswordPage />);

    submit("  person@example.com ");

    await waitFor(() => expect(screen.getByText("forgot.sent")).toBeTruthy());
    expect(mockRequestReset).toHaveBeenCalledWith("person@example.com");
    expect(mockToast).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("forgot.emailLabel")).toBeNull();
  });

  it("shows a rate-limit toast on 429 and keeps the form", async () => {
    mockRequestReset.mockRejectedValue(new FakeApiError(429));
    render(<ForgotPasswordPage />);

    submit("person@example.com");

    await waitFor(() => expect(mockToast).toHaveBeenCalledTimes(1));
    expect(mockToast.mock.calls[0][0]).toMatchObject({
      description: "tooManyRequests",
      variant: "destructive",
    });
    expect(screen.queryByText("forgot.sent")).toBeNull();
    expect(screen.getByLabelText("forgot.emailLabel")).toBeTruthy();
  });

  it("shows a generic toast on a network failure", async () => {
    mockRequestReset.mockRejectedValue(new Error("offline"));
    render(<ForgotPasswordPage />);

    submit("person@example.com");

    await waitFor(() =>
      expect(mockToast.mock.calls[0][0]).toMatchObject({
        description: "genericError",
      }),
    );
  });

  it("links back to sign in", () => {
    render(<ForgotPasswordPage />);
    expect(
      screen.getByRole("link", { name: "backToLogin" }).getAttribute("href"),
    ).toBe("/login");
  });
});
