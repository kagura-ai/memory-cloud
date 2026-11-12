/**
 * /signup/verify (#1734): the token is read once and stripped from the URL,
 * the preview names the address, the terms gate the submit, every answer of
 * `complete` has its own state, and the return path comes from this browser.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import enMessages from "@/messages/en.json";
import jaMessages from "@/messages/ja.json";

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length > 0 ? `${k}:${JSON.stringify(vars)}` : k,
}));

let mockToken: string | null = "tok_SECRET_verify";
const mockReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useSearchParams: () => ({
    get: (k: string) => (k === "token" ? mockToken : null),
  }),
  useRouter: () => ({ replace: mockReplace }),
  usePathname: () => "/signup/verify",
}));

let mockTermsVersion: string | null = null;
let mockSystemInfoPending = false;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () =>
    mockSystemInfoPending
      ? null
      : { features: {}, terms_version: mockTermsVersion },
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("@/components/common/LoadingState", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/common/LoadingState")
  >("@/components/common/LoadingState");
  return {
    ...actual,
    SpinnerLoading: ({ message }: { message?: string }) => (
      <div role="status">{message}</div>
    ),
  };
});

const { mockPreview, mockComplete, mockPeek, mockClear, FakeApiError } =
  vi.hoisted(() => {
    class FakeApiError extends Error {
      readonly status: number;
      readonly details?: Record<string, unknown>;
      constructor(status: number, details?: Record<string, unknown>) {
        super("fake");
        this.status = status;
        this.details = details;
      }
    }
    return {
      mockPreview: vi.fn(),
      mockComplete: vi.fn(),
      mockPeek: vi.fn(),
      mockClear: vi.fn(),
      FakeApiError,
    };
  });
vi.mock("@/lib/auth/emailSignup", () => ({
  previewEmailSignup: (...a: unknown[]) => mockPreview(...a),
  completeEmailSignup: (...a: unknown[]) => mockComplete(...a),
  peekSignupReturnTo: (...a: unknown[]) => mockPeek(...a),
  clearSignupReturnTo: (...a: unknown[]) => mockClear(...a),
}));
vi.mock("@/lib/api/base", () => ({ ApiError: FakeApiError }));

import SignupVerifyPage from "./page";

const GOOD = "New-Password-456!";
const FRONTEND_ORIGIN = "http://localhost:3000";
const originalLocation = window.location;
let hrefAssignments: string[] = [];
const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map(
  (level) => vi.spyOn(console, level),
);

function fillPassword() {
  fireEvent.change(screen.getByLabelText("newPassword"), {
    target: { value: GOOD },
  });
  fireEvent.change(screen.getByLabelText("confirmPassword"), {
    target: { value: GOOD },
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "verify.submit" }));
}

async function renderForm() {
  render(<SignupVerifyPage />);
  await screen.findByText(
    `verify.description:${JSON.stringify({ email: "new@example.com" })}`,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockToken = "tok_SECRET_verify";
  mockTermsVersion = null;
  mockSystemInfoPending = false;
  hrefAssignments = [];
  mockPreview.mockResolvedValue({
    email: "new@example.com",
    expires_at: "2030-01-01T00:00:00Z",
  });
  mockComplete.mockResolvedValue({
    redirect_url: `${FRONTEND_ORIGIN}/workspace/dashboard`,
  });
  mockPeek.mockReturnValue(undefined);
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get origin() {
        return FRONTEND_ORIGIN;
      },
      get href() {
        return `${FRONTEND_ORIGIN}/signup/verify`;
      },
      set href(val: string) {
        hrefAssignments.push(val);
      },
    },
  });
});

afterEach(() => {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  for (const spy of consoleSpies) {
    expect(JSON.stringify(spy.mock.calls)).not.toContain("tok_SECRET");
  }
});

describe("/signup/verify — the link", () => {
  it("reads the token once, strips it from the URL and previews it", async () => {
    await renderForm();
    expect(mockReplace).toHaveBeenCalledWith("/signup/verify", {
      scroll: false,
    });
    expect(mockPreview).toHaveBeenCalledWith("tok_SECRET_verify");
  });

  it("without a token → invalid, no preview", async () => {
    mockToken = null;
    render(<SignupVerifyPage />);
    expect(await screen.findByText("verify.invalidTitle")).toBeVisible();
    expect(mockPreview).not.toHaveBeenCalled();
  });

  it.each([400, 404])("preview %s → invalid", async (status) => {
    mockPreview.mockRejectedValue(new FakeApiError(status));
    render(<SignupVerifyPage />);
    expect(await screen.findByText("verify.invalidTitle")).toBeVisible();
  });

  it.each([429, 503, 0])(
    "preview %s → retryable error, and Retry previews again",
    async (status) => {
      mockPreview.mockRejectedValueOnce(new FakeApiError(status));
      render(<SignupVerifyPage />);
      fireEvent.click(
        await screen.findByRole("button", { name: "verify.retry" }),
      );
      await screen.findByText(
        `verify.description:${JSON.stringify({ email: "new@example.com" })}`,
      );
      expect(mockPreview).toHaveBeenCalledTimes(2);
    },
  );
});

describe("/signup/verify — creating the account", () => {
  it("creates the account with the kept return path and follows redirect_url", async () => {
    mockPeek.mockReturnValue("/invite/abc");
    await renderForm();
    fireEvent.change(screen.getByLabelText("verify.nameLabel"), {
      target: { value: "  Ada  " },
    });
    fillPassword();
    submit();

    await waitFor(() => expect(hrefAssignments).toHaveLength(1));
    expect(mockPeek).toHaveBeenCalledWith(FRONTEND_ORIGIN);
    expect(mockComplete).toHaveBeenCalledWith({
      token: "tok_SECRET_verify",
      newPassword: GOOD,
      name: "Ada",
      acceptedTerms: undefined,
      returnTo: "/invite/abc",
    });
    expect(mockClear).toHaveBeenCalled();
    expect(hrefAssignments[0]).toBe(`${FRONTEND_ORIGIN}/workspace/dashboard`);
  });

  it("asks for the terms when the deployment records them", async () => {
    mockTermsVersion = "2026-01";
    await renderForm();
    fillPassword();
    const button = screen.getByRole("button", { name: "verify.submit" });
    expect(button).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/ }));
    expect(button).toBeEnabled();
    submit();
    await waitFor(() => expect(mockComplete).toHaveBeenCalled());
    expect(mockComplete.mock.calls[0][0]).toMatchObject({
      acceptedTerms: "2026-01",
    });
  });

  it("waits for /system/info before it can submit", async () => {
    mockSystemInfoPending = true;
    await renderForm();
    fillPassword();
    expect(
      screen.getByRole("button", { name: "verify.submit" }),
    ).toBeDisabled();
  });

  it.each([
    [400, "verify.invalidTitle"],
    [403, "verify.notAllowedTitle"],
    [409, "verify.existsTitle"],
    [503, "verify.createdTitle"],
  ])("complete %s → %s", async (status, title) => {
    mockComplete.mockRejectedValue(new FakeApiError(status));
    await renderForm();
    fillPassword();
    submit();
    expect(await screen.findByText(title)).toBeVisible();
    expect(hrefAssignments).toEqual([]);
  });

  it("409 offers sign-in and the password reset", async () => {
    mockComplete.mockRejectedValue(new FakeApiError(409));
    await renderForm();
    fillPassword();
    submit();
    expect(
      await screen.findByRole("link", { name: "verify.signIn" }),
    ).toHaveAttribute("href", "/login");
    expect(
      screen.getByRole("link", { name: "verify.forgotPassword" }),
    ).toHaveAttribute("href", "/password/forgot");
  });

  it("422 on the password shows the policy message and keeps the form", async () => {
    mockComplete.mockRejectedValue(
      new FakeApiError(422, { field: "new_password" }),
    );
    await renderForm();
    fillPassword();
    submit();
    expect(await screen.findByText("policyError")).toBeVisible();
    expect(screen.getByRole("button", { name: "verify.submit" })).toBeVisible();
  });

  it("the terms message points to the emailed link, not a reload", () => {
    // A reload loses the token (it is removed from the address bar), and the
    // page then says the link is invalid or used though it still works.
    const en = enMessages.emailSignup.verify.termsRequired;
    const ja = jaMessages.emailSignup.verify.termsRequired;
    expect(en).not.toMatch(/reload/i);
    expect(en).toMatch(/link from the email/i);
    expect(ja).not.toContain("再読み込み");
    expect(ja).toContain("メールのリンク");
  });

  it("422 on the terms says so instead", async () => {
    mockComplete.mockRejectedValue(
      new FakeApiError(422, { field: "accepted_terms" }),
    );
    await renderForm();
    fillPassword();
    submit();
    expect(await screen.findByText("verify.termsRequired")).toBeVisible();
    expect(screen.queryByText("policyError")).toBeNull();
  });

  it.each([
    [429, "verify.tooManyRequests"],
    [500, "verify.genericError"],
  ])("%s → a toast, the form stays", async (status, description) => {
    mockComplete.mockRejectedValue(new FakeApiError(status));
    await renderForm();
    fillPassword();
    submit();
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ description }),
      ),
    );
    expect(screen.getByRole("button", { name: "verify.submit" })).toBeVisible();
  });
});
