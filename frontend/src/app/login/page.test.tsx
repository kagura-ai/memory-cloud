/**
 * Tests for LoginPage MFA Enter-key submit (Issue #484).
 *
 * The MFA TOTP form is a single-input form with a conditionally-disabled
 * submit button — browsers may suppress implicit form submission on Enter
 * when the button is disabled at keypress time. The component handles
 * Enter explicitly via onKeyDown; these tests guard that handler.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import LoginPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockGetAuthConfig = vi.fn();
const mockLoginWithPassword = vi.fn();
const mockVerifyMfa = vi.fn();

vi.mock("@/lib/auth/auth", () => ({
  getAuthUrl: vi.fn(),
  getGitHubAuthUrl: vi.fn(),
  getAuthConfig: (...args: unknown[]) => mockGetAuthConfig(...args),
  loginWithPassword: (...args: unknown[]) => mockLoginWithPassword(...args),
  verifyMfa: (...args: unknown[]) => mockVerifyMfa(...args),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

const mockPush = vi.fn();
// Stable URLSearchParams instance — LoginPage's useEffect lists `searchParams`
// in its dependency array, so a fresh instance per render would re-run the
// effect (and getAuthConfig() / state updates) unnecessarily during tests.
const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
  useSearchParams: () => mockSearchParams,
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));

// ---------- Helpers ----------------------------------------------------------

const SESSION_TOKEN = "mfa-session-token-stub";

beforeEach(() => {
  mockGetAuthConfig.mockReset();
  mockLoginWithPassword.mockReset();
  mockVerifyMfa.mockReset();
  mockPush.mockReset();
  // Clear URL params between tests so return_to from one test doesn't bleed
  for (const key of [...mockSearchParams.keys()]) {
    mockSearchParams.delete(key);
  }

  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: false,
    github_oauth_enabled: false,
  });
  mockLoginWithPassword.mockResolvedValue({
    mfa_required: true,
    mfa_session_token: SESSION_TOKEN,
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

/** Drive password → MFA-required transition and return the totp Input. */
async function reachMfaForm(): Promise<HTMLInputElement> {
  render(<LoginPage />);

  // Wait for auth config to resolve and admin password form to render.
  const loginIdInput = (await screen.findByLabelText(
    "loginId",
  )) as HTMLInputElement;
  const passwordInput = (await screen.findByLabelText(
    "password",
  )) as HTMLInputElement;

  fireEvent.change(loginIdInput, { target: { value: "admin@example.com" } });
  fireEvent.change(passwordInput, { target: { value: "hunter2" } });

  // Tick the terms checkbox (sign-in button is gated on it).
  const termsCheckbox = screen.getByRole("checkbox") as HTMLInputElement;
  fireEvent.click(termsCheckbox);

  // Submit password form.
  const signInButton = screen.getByRole("button", { name: "signIn" });
  fireEvent.click(signInButton);

  // Wait for MFA form to appear.
  return (await screen.findByLabelText("totpCode")) as HTMLInputElement;
}

// ---------- Tests ------------------------------------------------------------

describe("LoginPage MFA form — Enter key submit (#484)", () => {
  it("submits MFA verify when Enter is pressed after a 6-digit TOTP entry", async () => {
    mockVerifyMfa.mockResolvedValue({ redirect_url: null });

    const totpInput = await reachMfaForm();
    fireEvent.change(totpInput, { target: { value: "123456" } });
    fireEvent.keyDown(totpInput, { key: "Enter" });

    await waitFor(() => {
      expect(mockVerifyMfa).toHaveBeenCalledTimes(1);
    });
    expect(mockVerifyMfa).toHaveBeenCalledWith(
      SESSION_TOKEN,
      "123456",
      undefined,
    );
  });

  it("does NOT submit when Enter is pressed with fewer than 6 digits", async () => {
    mockVerifyMfa.mockResolvedValue({ redirect_url: null });

    const totpInput = await reachMfaForm();
    fireEvent.change(totpInput, { target: { value: "12345" } });
    fireEvent.keyDown(totpInput, { key: "Enter" });

    expect(mockVerifyMfa).not.toHaveBeenCalled();
  });

  it("does NOT re-submit when Enter is pressed while verify is in flight", async () => {
    // verifyMfa returns a pending promise so loadingAction stays "mfa".
    let resolveVerify!: (v: { redirect_url: string | null }) => void;
    mockVerifyMfa.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveVerify = resolve;
        }),
    );

    const totpInput = await reachMfaForm();
    fireEvent.change(totpInput, { target: { value: "123456" } });
    fireEvent.keyDown(totpInput, { key: "Enter" });

    await waitFor(() => {
      expect(mockVerifyMfa).toHaveBeenCalledTimes(1);
    });

    fireEvent.keyDown(totpInput, { key: "Enter" });
    expect(mockVerifyMfa).toHaveBeenCalledTimes(1);

    resolveVerify({ redirect_url: null });
  });
});

// ---------- return_to integration — safeReturnTo validation (#772) -----------

/**
 * Drive the password login form to submission and return the call args passed
 * to mockLoginWithPassword. The caller is responsible for setting up
 * mockSearchParams and mockLoginWithPassword before calling this helper.
 */
async function submitPasswordLogin(): Promise<unknown[]> {
  render(<LoginPage />);

  const loginIdInput = (await screen.findByLabelText(
    "loginId",
  )) as HTMLInputElement;
  const passwordInput = (await screen.findByLabelText(
    "password",
  )) as HTMLInputElement;

  fireEvent.change(loginIdInput, { target: { value: "user@example.com" } });
  fireEvent.change(passwordInput, { target: { value: "password123" } });

  const termsCheckbox = screen.getByRole("checkbox") as HTMLInputElement;
  fireEvent.click(termsCheckbox);

  const signInButton = screen.getByRole("button", { name: "signIn" });
  fireEvent.click(signInButton);

  await waitFor(() => {
    expect(mockLoginWithPassword).toHaveBeenCalledTimes(1);
  });

  return mockLoginWithPassword.mock.calls[0];
}

describe("LoginPage return_to sanitisation via safeReturnTo (#772)", () => {
  it("strips a cross-origin return_to before passing to loginWithPassword", async () => {
    // safeReturnTo should reject the cross-origin URL; loginWithPassword receives undefined.
    mockSearchParams.set("return_to", "https://evil.com/x");
    mockLoginWithPassword.mockResolvedValue({ mfa_required: false });

    const args = await submitPasswordLogin();
    // args: [loginId, password, returnTo]
    expect(args[2]).toBeUndefined();
  });

  it("passes a safe relative return_to through to loginWithPassword", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    mockLoginWithPassword.mockResolvedValue({ mfa_required: false });

    const args = await submitPasswordLogin();
    expect(args[2]).toBe("/device?user_code=ABC");
  });
});
