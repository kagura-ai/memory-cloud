/**
 * "Sign up with email" (#1734): the request form says the same thing for every
 * address, keeps the return path in this browser (never in the request) and
 * maps the only answers that are not 202 (422 shape, 429 rate limit).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length > 0 ? `${k}:${JSON.stringify(vars)}` : k,
}));

const { mockRequest, mockRemember, FakeApiError } = vi.hoisted(() => {
  class FakeApiError extends Error {
    readonly status: number;
    constructor(status: number) {
      super("fake");
      this.status = status;
    }
  }
  return { mockRequest: vi.fn(), mockRemember: vi.fn(), FakeApiError };
});
vi.mock("@/lib/auth/emailSignup", () => ({
  requestEmailSignup: (...a: unknown[]) => mockRequest(...a),
  rememberSignupReturnTo: (...a: unknown[]) => mockRemember(...a),
}));
vi.mock("@/lib/api/base", () => ({ ApiError: FakeApiError }));

import { EmailSignupForm } from "./EmailSignupForm";

const INVITE = "inv_SECRET_token_abcdefghij";

function typeAndSend(address: string) {
  fireEvent.change(screen.getByLabelText("request.emailLabel"), {
    target: { value: address },
  });
  fireEvent.click(screen.getByRole("button", { name: "request.submit" }));
}

beforeEach(() => {
  mockRequest.mockReset();
  mockRemember.mockReset();
  mockRequest.mockResolvedValue(undefined);
});

describe("EmailSignupForm", () => {
  it("starts as a toggle and opens the form on click", () => {
    render(<EmailSignupForm />);
    expect(screen.queryByLabelText("request.emailLabel")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "request.toggle" }));
    expect(screen.getByLabelText("request.emailLabel")).toBeVisible();
  });

  it("focuses the address when opened by its own toggle", () => {
    render(<EmailSignupForm />);
    fireEvent.click(screen.getByRole("button", { name: "request.toggle" }));
    expect(screen.getByLabelText("request.emailLabel")).toHaveFocus();
  });

  it("does not take focus when shown open on page load", () => {
    render(<EmailSignupForm defaultOpen />);
    expect(screen.getByLabelText("request.emailLabel")).not.toHaveFocus();
  });

  it("focuses the address when the page opens it from its own control", () => {
    render(<EmailSignupForm defaultOpen autoFocus />);
    expect(screen.getByLabelText("request.emailLabel")).toHaveFocus();
  });

  it("keeps the submit disabled until an address is typed", () => {
    render(<EmailSignupForm defaultOpen />);
    expect(
      screen.getByRole("button", { name: "request.submit" }),
    ).toBeDisabled();
  });

  it("sends the trimmed address with the invite and shows the neutral answer", async () => {
    render(<EmailSignupForm defaultOpen invite={INVITE} returnTo="/device" />);
    typeAndSend("  new@example.com ");

    expect(await screen.findByText("request.sentTitle")).toBeVisible();
    expect(mockRequest).toHaveBeenCalledWith({
      email: "new@example.com",
      invite: INVITE,
    });
    // The neutral message names the address typed, nothing else.
    expect(
      screen.getByText(
        `request.sentMessage:${JSON.stringify({ email: "new@example.com" })}`,
      ),
    ).toBeVisible();
  });

  it("keeps the return path in the browser before the request, never in it", async () => {
    render(<EmailSignupForm defaultOpen returnTo="/invite/abc" />);
    typeAndSend("new@example.com");
    await screen.findByText("request.sentTitle");
    expect(mockRemember).toHaveBeenCalledWith("/invite/abc");
    expect(mockRequest.mock.calls[0][0]).not.toHaveProperty("returnTo");
    expect(mockRemember.mock.invocationCallOrder[0]).toBeLessThan(
      mockRequest.mock.invocationCallOrder[0],
    );
  });

  it("clears an older return path when this page has none", async () => {
    render(<EmailSignupForm defaultOpen />);
    typeAndSend("new@example.com");
    await screen.findByText("request.sentTitle");
    expect(mockRemember).toHaveBeenCalledWith(undefined);
  });

  it.each([
    [422, "request.invalidEmail"],
    [429, "request.tooManyRequests"],
    [500, "request.genericError"],
    [0, "request.genericError"],
  ])("maps %s to %s and keeps the form", async (status, message) => {
    mockRequest.mockRejectedValue(new FakeApiError(status));
    render(<EmailSignupForm defaultOpen />);
    typeAndSend("new@example.com");
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByLabelText("request.emailLabel")).toBeVisible();
    expect(screen.queryByText("request.sentTitle")).toBeNull();
  });

  it("can start over with another address", async () => {
    render(<EmailSignupForm defaultOpen />);
    typeAndSend("new@example.com");
    fireEvent.click(
      await screen.findByRole("button", { name: "request.useAnother" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("request.emailLabel")).toHaveValue(""),
    );
  });
});
