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
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AuthProvider } from "@/contexts/AuthContext";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import LoginPage from "./page";

// ---------- Mocks ------------------------------------------------------------

const mockGetAuthConfig = vi.fn();
const mockLoginWithPassword = vi.fn();
const mockVerifyMfa = vi.fn();
const mockGetAuthUrl = vi.fn();
const mockGetGitHubAuthUrl = vi.fn();
// #1594: the session check. The real AuthProvider (mounted in the root layout,
// so it wraps /login in the app) calls this once on mount — GET /auth/me.
const mockGetCurrentUser = vi.fn();

vi.mock("@/lib/auth/auth", () => ({
  getAuthUrl: (...args: unknown[]) => mockGetAuthUrl(...args),
  getGitHubAuthUrl: (...args: unknown[]) => mockGetGitHubAuthUrl(...args),
  getAuthConfig: (...args: unknown[]) => mockGetAuthConfig(...args),
  loginWithPassword: (...args: unknown[]) => mockLoginWithPassword(...args),
  verifyMfa: (...args: unknown[]) => mockVerifyMfa(...args),
  getCurrentUser: (...args: unknown[]) => mockGetCurrentUser(...args),
  logout: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

const mockPush = vi.fn();
const mockReplace = vi.fn();
// Stable router object, like the real useRouter(): the #1594 forward effect
// lists `router` in its dependency array, so a fresh object per render would
// re-run it and make the replace() call counts below meaningless.
const mockRouter = { push: mockPush, replace: mockReplace };
// Stable URLSearchParams instance — LoginPage's useEffect lists `searchParams`
// in its dependency array, so a fresh instance per render would re-run the
// effect (and getAuthConfig() / state updates) unnecessarily during tests.
const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  useSearchParams: () => mockSearchParams,
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));

// ---------- Helpers ----------------------------------------------------------

const SESSION_TOKEN = "mfa-session-token-stub";

const SIGNED_IN_USER = {
  id: "user-1",
  email: "user@example.com",
  name: "Signed-in User",
};

/**
 * Render /login the way the app does: inside the real AuthProvider, which the
 * root layout mounts around every page. The page reads the session from that
 * provider (#1594), so every test goes through this helper.
 */
function renderLogin() {
  return render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
}

beforeEach(() => {
  mockGetAuthConfig.mockReset();
  mockLoginWithPassword.mockReset();
  mockVerifyMfa.mockReset();
  mockGetAuthUrl.mockReset();
  mockGetGitHubAuthUrl.mockReset();
  mockGetCurrentUser.mockReset();
  mockPush.mockReset();
  mockReplace.mockReset();
  // Clear URL params between tests so return_to from one test doesn't bleed
  for (const key of [...mockSearchParams.keys()]) {
    mockSearchParams.delete(key);
  }

  // Signed out by default (#1594): GET /auth/me answers 401, which
  // getCurrentUser() reports as null — the ordinary /login visitor. Shared
  // here so the pre-#1594 tests below reach the form without each one
  // restating it; the "live session" block overrides it per test.
  mockGetCurrentUser.mockResolvedValue(null);

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
  // The session-check timeout test (#1594) installs fake timers.
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** Drive password → MFA-required transition and return the totp Input. */
async function reachMfaForm(): Promise<HTMLInputElement> {
  renderLogin();

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
  renderLogin();

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
 * Render LoginPage, tick the terms checkbox, click the OAuth provider button.
 * Caller asserts on mockGetAuthUrl / mockGetGitHubAuthUrl + window.location.href
 * after the click. Caller sets up mockSearchParams + mocks beforehand.
 */
async function clickOAuthButton(provider: "google" | "github"): Promise<void> {
  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: provider === "google",
    github_oauth_enabled: provider === "github",
  });

  renderLogin();

  const buttonName =
    provider === "google" ? /continueWithGoogle/i : /continueWithGitHub/i;
  const button = await screen.findByRole("button", { name: buttonName });

  const termsCheckbox = screen.getByRole("checkbox", {
    name: /agreeToTerms/i,
  }) as HTMLInputElement;
  fireEvent.click(termsCheckbox);

  fireEvent.click(button);
}

describe("LoginPage OAuth failure banners (#1381)", () => {
  // The backend redirects non-cancel callback failures here with a
  // well-known error token — the page must map it to an i18n'd banner,
  // never render the raw token, and keep the cancel notice separate.
  it("shows the failed banner for ?error=oauth_failed", async () => {
    mockSearchParams.set("error", "oauth_failed");
    renderLogin();

    expect(await screen.findByText("oauthFailed")).toBeTruthy();
    // The raw token itself is not rendered as banner text.
    expect(screen.queryByText("oauth_failed")).toBeNull();
  });

  it("shows the expired banner for ?error=oauth_expired", async () => {
    mockSearchParams.set("error", "oauth_expired");
    renderLogin();

    expect(await screen.findByText("oauthExpired")).toBeTruthy();
    expect(screen.queryByText("oauth_expired")).toBeNull();
  });

  it("keeps the cancelled notice separate from the failure banner", async () => {
    mockSearchParams.set("cancelled", "1");
    renderLogin();

    expect(await screen.findByText("signinCancelled")).toBeTruthy();
    expect(screen.queryByText("oauthFailed")).toBeNull();
  });
});

describe("LoginPage OAuth return_to forwarding (#774)", () => {
  // Capture window.location.href assignments. Backend switches /api/v1/auth/
  // {provider}/login between JSON (no return_to) and 303 redirect (with
  // return_to), so the frontend must NOT route the redirect mode through
  // apiClient.get() — it goes through direct browser navigation instead.
  let hrefAssignments: string[];
  const originalLocation = window.location;
  const originalOrigin = window.location.origin;

  beforeEach(() => {
    hrefAssignments = [];
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        get origin() {
          return originalOrigin;
        },
        get href() {
          return hrefAssignments[hrefAssignments.length - 1] || "";
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
    vi.unstubAllEnvs();
  });

  it("with safe relative return_to, navigates directly to backend with absolute same-origin return_to (Google)", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    await clickOAuthButton("google");

    // Direct browser navigation — apiClient JSON path bypassed.
    // return_to is sent as an absolute same-origin URL (not the raw relative path)
    // so backend's verbatim RedirectResponse resolves against the frontend origin,
    // not the API origin. See buildOAuthRedirect JSDoc in page.tsx for the trap.
    await waitFor(() => {
      expect(hrefAssignments.length).toBeGreaterThan(0);
    });
    const expectedReturnTo = encodeURIComponent(
      new URL("/device?user_code=ABC", originalOrigin).toString(),
    );
    expect(hrefAssignments[0]).toMatch(
      new RegExp(`/api/v1/auth/google/login\\?return_to=${expectedReturnTo}$`),
    );
    // Guard against the /api/v1 double-prefix trap — verify the URL has
    // exactly one /api/v1/ segment, not two.
    expect(hrefAssignments[0]).not.toMatch(/\/api\/v1\/api\/v1\//);
    expect(mockGetAuthUrl).not.toHaveBeenCalled();
  });

  it("with safe relative return_to, navigates directly to backend with absolute same-origin return_to (GitHub)", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    await clickOAuthButton("github");

    await waitFor(() => {
      expect(hrefAssignments.length).toBeGreaterThan(0);
    });
    const expectedReturnTo = encodeURIComponent(
      new URL("/device?user_code=ABC", originalOrigin).toString(),
    );
    expect(hrefAssignments[0]).toMatch(
      new RegExp(`/api/v1/auth/github/login\\?return_to=${expectedReturnTo}$`),
    );
    expect(hrefAssignments[0]).not.toMatch(/\/api\/v1\/api\/v1\//);
    expect(mockGetGitHubAuthUrl).not.toHaveBeenCalled();
  });

  it("normalizes NEXT_PUBLIC_API_URL when it includes a trailing /api/v1 suffix", async () => {
    // Some deployments bake the version suffix into the env var. The helper
    // must strip it so the result is .../api/v1/auth/... not .../api/v1/api/v1/auth/...
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/api/v1");
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    await clickOAuthButton("google");

    await waitFor(() => {
      expect(hrefAssignments.length).toBeGreaterThan(0);
    });
    expect(hrefAssignments[0]).toMatch(
      /^https:\/\/api\.example\.com\/api\/v1\/auth\/google\/login\?return_to=/,
    );
    expect(hrefAssignments[0]).not.toMatch(/\/api\/v1\/api\/v1\//);
  });

  it("normalizes NEXT_PUBLIC_API_URL with a trailing slash", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/");
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    await clickOAuthButton("google");

    await waitFor(() => {
      expect(hrefAssignments.length).toBeGreaterThan(0);
    });
    expect(hrefAssignments[0]).toMatch(
      /^https:\/\/api\.example\.com\/api\/v1\/auth\/google\/login\?return_to=/,
    );
    expect(hrefAssignments[0]).not.toMatch(/\/\/api\/v1/);
  });

  it("with cross-origin return_to, sanitizes to undefined and uses JSON path (Google)", async () => {
    // safeReturnTo strips the cross-origin URL → returnTo is undefined →
    // we take the JSON path with no args (existing behavior).
    mockSearchParams.set("return_to", "https://evil.com/x");
    mockGetAuthUrl.mockResolvedValue("https://accounts.google.com/oauth/auth");
    await clickOAuthButton("google");

    await waitFor(() => {
      expect(mockGetAuthUrl).toHaveBeenCalledTimes(1);
    });
    expect(mockGetAuthUrl).toHaveBeenCalledWith();
  });

  it("with no return_to, uses JSON path with no args (Google)", async () => {
    mockGetAuthUrl.mockResolvedValue("https://accounts.google.com/oauth/auth");
    await clickOAuthButton("google");

    await waitFor(() => {
      expect(mockGetAuthUrl).toHaveBeenCalledTimes(1);
    });
    expect(mockGetAuthUrl).toHaveBeenCalledWith();
  });
});

// ---------- A live session is forwarded away from /login (#1594) -------------

/**
 * A signed-in visitor must never see the login form: signing in a second time
 * invalidates the session they already hold (#114). The session comes from the
 * AuthProvider — the same source the (authenticated) layout guard trusts — so
 * /login and that guard cannot disagree and bounce a visitor between them.
 */
describe("LoginPage forwards a live session (#1594)", () => {
  /** True once the login form (not the session-check placeholder) is on screen. */
  const formIsRendered = () =>
    screen.queryByRole("heading", { name: "signInToAccount" }) !== null;

  it("replaces to the dashboard without ever rendering the form", async () => {
    mockGetCurrentUser.mockResolvedValue(SIGNED_IN_USER);
    const sawForm: boolean[] = [];
    const observer = new MutationObserver(() => sawForm.push(formIsRendered()));
    observer.observe(document.body, { childList: true, subtree: true });

    renderLogin();
    sawForm.push(formIsRendered());

    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalledWith("/workspace/dashboard");
    });
    observer.disconnect();

    expect(mockReplace).toHaveBeenCalledTimes(1);
    // replace, not push: Back must not return to a page that bounces forward.
    expect(mockPush).not.toHaveBeenCalled();
    // Not before the check settled, not while the navigation is in flight.
    expect(sawForm).not.toContain(true);
    expect(formIsRendered()).toBe(false);
    expect(screen.getByRole("status")).toHaveTextContent("checkingSession");
  });

  it("replaces to a safe return_to instead of the default", async () => {
    mockGetCurrentUser.mockResolvedValue(SIGNED_IN_USER);
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    renderLogin();

    await waitFor(() => {
      expect(mockReplace).toHaveBeenCalledWith("/device?user_code=ABC");
    });
    expect(mockReplace).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["protocol-relative", "//evil.example"],
    ["cross-origin absolute", "https://evil.example/x"],
    ["javascript: scheme", "javascript:alert(1)"],
  ])(
    "falls back to the dashboard for a hostile return_to (%s)",
    async (_label, hostile) => {
      mockGetCurrentUser.mockResolvedValue(SIGNED_IN_USER);
      mockSearchParams.set("return_to", hostile);
      renderLogin();

      await waitFor(() => {
        expect(mockReplace).toHaveBeenCalledTimes(1);
      });
      // Only ever the sanitized value — the raw parameter never reaches replace().
      expect(mockReplace).toHaveBeenCalledWith("/workspace/dashboard");
    },
  );

  it.each([
    ["error=email_in_use", { error: "email_in_use" }, "emailInUse"],
    ["error=oauth_failed", { error: "oauth_failed" }, "oauthFailed"],
    ["an unknown error value", { error: "something_new" }, "something_new"],
    ["cancelled=1", { cancelled: "1" }, "signinCancelled"],
  ])(
    "with %s the banner wins: no forward, form rendered at once",
    async (_label, params, bannerText) => {
      // e.g. email_in_use arrives while ANOTHER account's session is live.
      mockGetCurrentUser.mockResolvedValue(SIGNED_IN_USER);
      for (const [key, value] of Object.entries(params)) {
        mockSearchParams.set(key, value);
      }
      renderLogin();

      // Rendered on the first pass — not held back behind the session check.
      expect(formIsRendered()).toBe(true);
      expect(screen.queryByRole("status")).toBeNull();
      expect(await screen.findByText(bannerText)).toBeTruthy();

      // Let the session check settle as signed-in: still no navigation.
      await waitFor(() => expect(mockGetCurrentUser).toHaveBeenCalled());
      await act(async () => {});
      expect(mockReplace).not.toHaveBeenCalled();
      expect(mockPush).not.toHaveBeenCalled();
      expect(formIsRendered()).toBe(true);
    },
  );

  it("shows an accessible placeholder, then the form, for a signed-out visitor (401)", async () => {
    // beforeEach default: getCurrentUser() → null, i.e. /auth/me said 401.
    renderLogin();

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("checkingSession");
    expect(formIsRendered()).toBe(false);
    // frontend/e2e/fixtures.ts gotoAndWaitStable() waits on "h1, form, main
    // button, main" before running axe. If the placeholder matched that, the
    // hermetic /login contrast spec would audit a spinner and pass vacuously.
    expect(document.querySelector("h1, form, main")).toBeNull();

    expect(
      await screen.findByRole("heading", { name: "signInToAccount" }),
    ).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    expect(mockReplace).not.toHaveBeenCalled();
    // One /auth/me per page view — the AuthProvider's. No second probe.
    expect(mockGetCurrentUser).toHaveBeenCalledTimes(1);
  });

  it("fails open to the form when the session check rejects (5xx / network)", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    mockGetCurrentUser.mockRejectedValue(
      Object.assign(new Error("Service Unavailable"), { status: 503 }),
    );
    renderLogin();

    expect(
      await screen.findByRole("heading", { name: "signInToAccount" }),
    ).toBeTruthy();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("fails open to the form when the session check never settles", async () => {
    vi.useFakeTimers();
    let settleCheck!: (user: typeof SIGNED_IN_USER | null) => void;
    mockGetCurrentUser.mockImplementation(
      () =>
        new Promise((resolve) => {
          settleCheck = resolve;
        }),
    );
    renderLogin();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_900);
    });
    expect(formIsRendered()).toBe(false);
    expect(screen.getByRole("status")).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    // A broken /auth/me must never lock anyone out of the login page.
    expect(formIsRendered()).toBe(true);
    expect(screen.queryByRole("status")).toBeNull();
    expect(mockReplace).not.toHaveBeenCalled();

    // The slow answer finally says "signed in": forward after all — leaving
    // the form up would let them sign in again and lose that session.
    await act(async () => {
      settleCheck(SIGNED_IN_USER);
    });
    expect(mockReplace).toHaveBeenCalledTimes(1);
    expect(mockReplace).toHaveBeenCalledWith("/workspace/dashboard");
    expect(formIsRendered()).toBe(false);
  });

  it("clears the timeout when the check settles, and on unmount", async () => {
    vi.useFakeTimers();

    // Settled (signed out): nothing left ticking behind the form.
    const settled = renderLogin();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(formIsRendered()).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    settled.unmount();

    // Unmounted mid-check: the pending timer goes with the page.
    mockGetCurrentUser.mockImplementation(() => new Promise(() => {}));
    const pending = renderLogin();
    expect(vi.getTimerCount()).toBe(1);
    pending.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("leaves the password path's own post-login navigation alone", async () => {
    // Signed out → form → password login succeeds with no redirect_url. The
    // page's existing router.push owns this hop; the forward effect must not
    // also fire (the AuthProvider is not refetched by a password login).
    mockLoginWithPassword.mockResolvedValue({ mfa_required: false });
    await submitPasswordLogin();

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith("/workspace/dashboard");
    });
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("keeps the dev mock-auth redirect as it was", async () => {
    // In mock mode the AuthProvider hands out a mock user. Unguarded, the
    // forward would replace() to the dashboard on top of the existing push.
    vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("NEXT_PUBLIC_ENABLE_MOCK_AUTH", "true");
    try {
      renderLogin();

      await waitFor(() => {
        expect(mockPush).toHaveBeenCalledWith("/workspace/contexts");
      });
      await act(async () => {});
      expect(mockReplace).not.toHaveBeenCalled();
      expect(screen.getByText("mockAuthEnabled")).toBeTruthy();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("has the placeholder label in both catalogs", () => {
    // Component tests mock useTranslations, so a missing key would not show.
    expect(en.login.checkingSession).toBeTruthy();
    expect(ja.login.checkingSession).toBeTruthy();
  });
});
