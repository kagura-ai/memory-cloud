/**
 * Reset / set-up link landing page (#1678): token handling, the error
 * mapping (400 → invalid link, 422 → field message, other → toast) and the
 * success destinations.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockT = (key: string) => key;
vi.mock("next-intl", () => ({ useTranslations: () => mockT }));

let mockToken: string | null = "tok-1";
const mockSearchParams = {
  get: (k: string) => (k === "token" ? mockToken : null),
};
vi.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const { mockReset, mockSetup, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number;
    constructor(status: number) {
      super("fake");
      this.status = status;
    }
  }
  return { mockReset: vi.fn(), mockSetup: vi.fn(), FakeApiError };
});
vi.mock("@/lib/auth/auth", () => ({
  resetPassword: (...a: unknown[]) => mockReset(...a),
  setupPassword: (...a: unknown[]) => mockSetup(...a),
}));
vi.mock("@/lib/api/base", () => ({ ApiError: FakeApiError }));

import { PasswordLinkPage } from "./PasswordLinkPage";

const GOOD = "New-Password-456!";

function fill(newPassword: string, confirm: string) {
  fireEvent.change(screen.getByLabelText("newPassword"), {
    target: { value: newPassword },
  });
  fireEvent.change(screen.getByLabelText("confirmPassword"), {
    target: { value: confirm },
  });
}

beforeEach(() => {
  mockToken = "tok-1";
  mockReset.mockReset();
  mockSetup.mockReset();
  mockToast.mockReset();
});

describe("PasswordLinkPage — reset", () => {
  it("resets with the token and points at sign in", async () => {
    mockReset.mockResolvedValue(undefined);
    render(<PasswordLinkPage mode="reset" />);

    fill(GOOD, GOOD);
    fireEvent.click(screen.getByRole("button", { name: "reset.submit" }));

    await waitFor(() =>
      expect(screen.getByText("reset.successTitle")).toBeTruthy(),
    );
    expect(mockReset).toHaveBeenCalledWith("tok-1", GOOD);
    expect(
      screen.getByRole("link", { name: "reset.signIn" }).getAttribute("href"),
    ).toBe("/login");
  });

  it("refuses a mismatch before calling the backend", () => {
    render(<PasswordLinkPage mode="reset" />);
    fill(GOOD, GOOD + "x");
    fireEvent.click(screen.getByRole("button", { name: "reset.submit" }));
    expect(screen.getByText("mismatch")).toBeTruthy();
    expect(mockReset).not.toHaveBeenCalled();
  });

  it("shows the invalid-link state on 400", async () => {
    mockReset.mockRejectedValue(new FakeApiError(400));
    render(<PasswordLinkPage mode="reset" />);
    fill(GOOD, GOOD);
    fireEvent.click(screen.getByRole("button", { name: "reset.submit" }));

    await waitFor(() =>
      expect(screen.getByText("reset.invalidTitle")).toBeTruthy(),
    );
    expect(
      screen
        .getByRole("link", { name: "reset.requestNew" })
        .getAttribute("href"),
    ).toBe("/password/forgot");
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("shows the policy message next to the field on 422", async () => {
    mockReset.mockRejectedValue(new FakeApiError(422));
    render(<PasswordLinkPage mode="reset" />);
    fill("weak", "weak");
    fireEvent.click(screen.getByRole("button", { name: "reset.submit" }));

    await waitFor(() => expect(screen.getByText("policyError")).toBeTruthy());
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("toasts a rate limit", async () => {
    mockReset.mockRejectedValue(new FakeApiError(429));
    render(<PasswordLinkPage mode="reset" />);
    fill(GOOD, GOOD);
    fireEvent.click(screen.getByRole("button", { name: "reset.submit" }));

    await waitFor(() =>
      expect(mockToast.mock.calls[0][0]).toMatchObject({
        description: "tooManyRequests",
        variant: "destructive",
      }),
    );
  });

  it("without a token shows the invalid state and no form", () => {
    mockToken = null;
    render(<PasswordLinkPage mode="reset" />);
    expect(screen.getByText("reset.invalidTitle")).toBeTruthy();
    expect(screen.queryByLabelText("newPassword")).toBeNull();
  });
});

describe("PasswordLinkPage — setup", () => {
  it("sets the password and points at the profile", async () => {
    mockSetup.mockResolvedValue(undefined);
    render(<PasswordLinkPage mode="setup" />);

    fill(GOOD, GOOD);
    fireEvent.click(screen.getByRole("button", { name: "setup.submit" }));

    await waitFor(() =>
      expect(screen.getByText("setup.successTitle")).toBeTruthy(),
    );
    expect(mockSetup).toHaveBeenCalledWith("tok-1", GOOD);
    expect(mockReset).not.toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: "setup.continue" }).getAttribute("href"),
    ).toBe("/profile");
  });

  it("shows its own invalid-link copy on 400", async () => {
    mockSetup.mockRejectedValue(new FakeApiError(400));
    render(<PasswordLinkPage mode="setup" />);
    fill(GOOD, GOOD);
    fireEvent.click(screen.getByRole("button", { name: "setup.submit" }));
    await waitFor(() =>
      expect(screen.getByText("setup.invalidTitle")).toBeTruthy(),
    );
  });
});
