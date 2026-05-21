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
const mockGetAuthUrl = vi.fn();
const mockGetGitHubAuthUrl = vi.fn();

vi.mock("@/lib/auth/auth", () => ({
  getAuthUrl: (...args: unknown[]) => mockGetAuthUrl(...args),
  getGitHubAuthUrl: (...args: unknown[]) => mockGetGitHubAuthUrl(...args),
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
  mockGetAuthUrl.mockReset();
  mockGetGitHubAuthUrl.mockReset();
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
  // Use name-scoped query to survive future renders that add a second checkbox
  // (e.g. the OAuth-path terms checkbox in the same component tree).
  const termsCheckbox = screen.getByRole("checkbox", {
    name: /agreeToTerms/i,
  }) as HTMLInputElement;
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

  // Use name-scoped query for resilience against multiple checkboxes.
  const termsCheckbox = screen.getByRole("checkbox", {
    name: /agreeToTerms/i,
  }) as HTMLInputElement;
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

  it("forwards a safe return_to through the MFA path to verifyMfa", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABC");
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
      "/device?user_code=ABC",
    );
    expect(mockLoginWithPassword.mock.calls[0][2]).toBe(
      "/device?user_code=ABC",
    );
  });
});

// ---------- OAuth return_to forwarding (#774) -------------------------------

/**
 * Click an OAuth provider button and return the args passed to its
 * getAuthUrl/getGitHubAuthUrl mock. Caller sets up mockSearchParams +
 * mockGetAuthUrl/mockGetGitHubAuthUrl.mockResolvedValue() before calling.
 */
async function clickOAuthButton(
  provider: "google" | "github",
): Promise<unknown[]> {
  // Enable the OAuth path in the auth config.
  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: provider === "google",
    github_oauth_enabled: provider === "github",
  });

  render(<LoginPage />);

  const buttonName =
    provider === "google" ? /continueWithGoogle/i : /continueWithGitHub/i;
  const button = await screen.findByRole("button", { name: buttonName });

  // Terms checkbox gates the OAuth buttons; check it before clicking the provider button.
  const termsCheckbox = screen.getByRole("checkbox", {
    name: /agreeToTerms/i,
  }) as HTMLInputElement;
  fireEvent.click(termsCheckbox);

  fireEvent.click(button);

  const mock = provider === "google" ? mockGetAuthUrl : mockGetGitHubAuthUrl;
  await waitFor(() => {
    expect(mock).toHaveBeenCalledTimes(1);
  });
  return mock.mock.calls[0];
}

describe("LoginPage OAuth return_to forwarding (#774)", () => {
  it("forwards a safe relative return_to to getAuthUrl (Google)", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    mockGetAuthUrl.mockResolvedValue("https://accounts.google.com/oauth/auth");

    const args = await clickOAuthButton("google");
    expect(args[0]).toBe("/device?user_code=ABC");
  });

  it("forwards a safe relative return_to to getGitHubAuthUrl (GitHub)", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    mockGetGitHubAuthUrl.mockResolvedValue(
      "https://github.com/login/oauth/authorize",
    );

    const args = await clickOAuthButton("github");
    expect(args[0]).toBe("/device?user_code=ABC");
  });

  it("strips a cross-origin return_to before passing to getAuthUrl (Google)", async () => {
    // safeReturnTo should reject the cross-origin URL; getAuthUrl receives undefined.
    mockSearchParams.set("return_to", "https://evil.com/x");
    mockGetAuthUrl.mockResolvedValue("https://accounts.google.com/oauth/auth");

    const args = await clickOAuthButton("google");
    expect(args[0]).toBeUndefined();
  });

  it("passes undefined when no return_to is present (Google)", async () => {
    // No mockSearchParams.set → returnTo is undefined → getAuthUrl(undefined).
    mockGetAuthUrl.mockResolvedValue("https://accounts.google.com/oauth/auth");

    const args = await clickOAuthButton("google");
    expect(args[0]).toBeUndefined();
  });
});
