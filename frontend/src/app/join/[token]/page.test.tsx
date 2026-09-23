/**
 * /join/[token] — the beta invite landing page (#1582).
 *
 * Six outcomes from two probes (session, preview) plus the feature flag, and
 * the one thing the page exists for: carrying the token into the OAuth login.
 * The token is a credential — nothing here may log or persist it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockPreview = vi.fn();
const mockApiClientGet = vi.fn();
const mockGetAuthConfig = vi.fn();

vi.mock("@/lib/api/beta-invites", () => ({
  previewBetaInvite: (...args: unknown[]) => mockPreview(...args),
}));

vi.mock("@/lib/api/base", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/base")>("@/lib/api/base");
  return {
    ...actual,
    apiClient: {
      get: (...args: unknown[]) => mockApiClientGet(...args),
    },
  };
});

vi.mock("@/lib/auth/auth", () => ({
  getAuthConfig: () => mockGetAuthConfig(),
}));

let mockFeatures: Record<string, boolean> | null = { beta_invites: true };
// #1665: the terms version /system/info reports; null = not recorded.
let mockTermsVersion: string | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
  useSystemInfo: () =>
    mockFeatures === null
      ? null
      : { features: mockFeatures, terms_version: mockTermsVersion },
}));

vi.mock("next-intl", () => ({
  useLocale: () => "en",
  useTranslations: () => (k: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length > 0 ? `${k}:${JSON.stringify(vars)}` : k,
}));

// #1655: /join reads an optional return_to. One stable instance, cleared in
// beforeEach, like the /login tests.
const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));

vi.mock("@/components/common/LoadingState", () => ({
  SpinnerLoading: ({ message }: { message?: string }) => (
    <div role="status">{message}</div>
  ),
}));

// React.use(params) suspends in test envs even with Promise.resolve(); mock it
// to unwrap a plain object synchronously.
vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return {
    ...actual,
    use: <T,>(value: T | Promise<T>): T => value as T,
  };
});

import { ApiError } from "@/lib/api/base";
import JoinPage from "./page";

const TOKEN = "tok_SECRET-join/1";
const FRONTEND_ORIGIN = "http://localhost:3000";
const originalLocation = window.location;
let hrefAssignments: string[] = [];

const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map(
  (level) => vi.spyOn(console, level),
);

/** Tick the terms box — both provider buttons stay disabled until then (#1655). */
async function agreeToTerms() {
  fireEvent.click(await screen.findByRole("checkbox", { name: /agreeToTerms/ }));
}

function renderPage() {
  // params type is Promise<{token}>; the react.use mock above unwraps plain values.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return render(<JoinPage params={{ token: TOKEN } as any} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
  hrefAssignments = [];
  mockFeatures = { beta_invites: true };
  mockTermsVersion = null;
  for (const key of [...mockSearchParams.keys()]) {
    mockSearchParams.delete(key);
  }

  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get origin() {
        return FRONTEND_ORIGIN;
      },
      get pathname() {
        return `/join/${TOKEN}`;
      },
      get search() {
        return "";
      },
      get href() {
        return `${FRONTEND_ORIGIN}/join/${TOKEN}`;
      },
      set href(val: string) {
        hrefAssignments.push(val);
      },
    },
  });

  mockApiClientGet.mockRejectedValue(new Error("Not authenticated"));
  mockPreview.mockResolvedValue({
    valid: true,
    expires_at: "2030-01-08T00:00:00Z",
  });
  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: true,
    github_oauth_enabled: true,
  });
});

afterEach(() => {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  vi.unstubAllEnvs();

  for (const spy of consoleSpies) {
    expect(JSON.stringify(spy.mock.calls)).not.toContain("tok_SECRET");
  }
  expect(
    JSON.stringify([{ ...window.localStorage }, { ...window.sessionStorage }]),
  ).not.toContain("tok_SECRET");
});

describe("/join/[token] — valid", () => {
  it("starts in loading, then shows the invitation with its expiry", async () => {
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent("join.loading");

    expect(
      await screen.findByRole("heading", { name: "join.valid.title" }),
    ).toBeVisible();
    expect(screen.getByText(/^join\.valid\.expires:/)).toBeVisible();
    expect(mockPreview).toHaveBeenCalledWith(TOKEN);
  });

  it("offers only the providers the deployment has", async () => {
    mockGetAuthConfig.mockResolvedValue({
      password_login_enabled: true,
      google_oauth_enabled: false,
      github_oauth_enabled: true,
    });
    renderPage();
    expect(
      await screen.findByRole("button", {
        name: "join.valid.continueWithGitHub",
      }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "join.valid.continueWithGoogle" }),
    ).toBeNull();
  });

  it("says so when no sign-up provider is configured (or the probe fails)", async () => {
    mockGetAuthConfig.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByText("join.valid.noProviders")).toBeVisible();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it.each([
    ["google", "join.valid.continueWithGoogle"],
    ["github", "join.valid.continueWithGitHub"],
  ])(
    "%s sign-up carries return_to and the invite token into the OAuth login",
    async (provider, label) => {
      vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
      renderPage();
      await agreeToTerms();
      fireEvent.click(await screen.findByRole("button", { name: label }));

      await waitFor(() => expect(hrefAssignments).toHaveLength(1));
      // #1594: the invitee comes back to the dashboard, not to "/" — the site
      // root only redirects to /login, which showed a signed-in person the
      // login form again.
      expect(hrefAssignments[0]).toBe(
        `https://api.example.com/api/v1/auth/${provider}/login` +
          `?return_to=${encodeURIComponent(`${FRONTEND_ORIGIN}/workspace/dashboard`)}` +
          `&invite=${encodeURIComponent(TOKEN)}`,
      );
    },
  );
});

describe("/join/[token] — not usable", () => {
  it("404 with the feature on → invalid", async () => {
    mockPreview.mockRejectedValue(
      new ApiError({ message: "not found", status: 404 }),
    );
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "join.invalid.title" }),
    ).toBeVisible();
  });

  it.each([
    ["the flag is off", { beta_invites: false }],
    ["an older backend sends no flag", { plan_page: true }],
  ])("404 when %s → disabled", async (_label, features) => {
    mockFeatures = features;
    mockPreview.mockRejectedValue(
      new ApiError({ message: "not found", status: 404 }),
    );
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "join.disabled.title" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "join.invalid.title" }),
    ).toBeNull();
  });

  it("404 stays in loading until the flags are known — no invalid→disabled flip", async () => {
    mockFeatures = null;
    mockPreview.mockRejectedValue(
      new ApiError({ message: "not found", status: 404 }),
    );
    const view = renderPage();
    await waitFor(() => expect(mockPreview).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("join.loading"),
    );
    expect(screen.queryByRole("heading")).toBeNull();

    mockFeatures = {};
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    view.rerender(<JoinPage params={{ token: TOKEN } as any} />);
    expect(
      await screen.findByRole("heading", { name: "join.disabled.title" }),
    ).toBeVisible();
  });

  it("410 → expired", async () => {
    mockPreview.mockRejectedValue(
      new ApiError({ message: "gone", status: 410 }),
    );
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "join.expired.title" }),
    ).toBeVisible();
  });
});

describe("/join/[token] — could not check", () => {
  // None of these says anything about the link: 429 is the preview's own
  // per-IP rate limit, and telling the invitee a live link is dead sends them
  // back to the inviter for a new one.
  it.each([
    ["a network failure", 0],
    ["429 rate limited", 429],
    ["a 5xx", 503],
    ["an unexpected 401", 401],
  ])("%s → retryable error, never invalid", async (_label, status) => {
    mockPreview.mockRejectedValue(new ApiError({ message: "nope", status }));
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "join.error.title" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "join.invalid.title" }),
    ).toBeNull();
    expect(
      screen.getByRole("link", { name: "join.backToLogin" }),
    ).toHaveAttribute("href", "/login");
  });

  it("does not wait for the feature flags, and ignores them", async () => {
    mockFeatures = null;
    mockPreview.mockRejectedValue(
      new ApiError({ message: "Network error", status: 0 }),
    );
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "join.error.title" }),
    ).toBeVisible();
  });

  it("Retry runs the probe again and can land on the invitation", async () => {
    mockPreview.mockRejectedValueOnce(
      new ApiError({ message: "Too Many Requests", status: 429 }),
    );
    renderPage();
    fireEvent.click(
      await screen.findByRole("button", { name: "join.error.retry" }),
    );

    expect(
      await screen.findByRole("heading", { name: "join.valid.title" }),
    ).toBeVisible();
    expect(mockPreview).toHaveBeenCalledTimes(2);
    expect(mockPreview).toHaveBeenLastCalledWith(TOKEN);
  });
});

describe("/join/[token] — already signed in", () => {
  it("links to the dashboard and leaves the token untouched", async () => {
    mockApiClientGet.mockResolvedValue({
      user_id: "u1",
      email: "a@example.com",
      name: "A",
    });
    renderPage();
    expect(
      await screen.findByRole("heading", {
        name: "join.alreadySignedIn.title",
      }),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "join.alreadySignedIn.goToDashboard" }),
    ).toHaveAttribute("href", "/workspace/dashboard");
    expect(mockApiClientGet).toHaveBeenCalledWith("/api/v1/auth/me");
    expect(mockPreview).not.toHaveBeenCalled();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

// ---------- #1655: return_to and terms --------------------------------------

const PROVIDERS = [
  ["google", "join.valid.continueWithGoogle"],
  ["github", "join.valid.continueWithGitHub"],
] as const;

function loginUrl(provider: string, returnTo: string) {
  return (
    `https://api.example.com/api/v1/auth/${provider}/login` +
    `?return_to=${encodeURIComponent(returnTo)}` +
    `&invite=${encodeURIComponent(TOKEN)}`
  );
}

async function signUpWith(label: string) {
  renderPage();
  await agreeToTerms();
  fireEvent.click(await screen.findByRole("button", { name: label }));
  await waitFor(() => expect(hrefAssignments).toHaveLength(1));
  return hrefAssignments[0];
}

// Each of these must fall back to the dashboard, silently.
const UNSAFE_RETURN_TO = [
  ["a cross-origin URL", "https://evil.example/device"],
  ["a protocol-relative URL", "//evil.example"],
  ["a backslash", "/\\evil.example"],
  ["a TAB", "/\t/evil.example"],
  ["an LF", "/device\n?user_code=X"],
  ["a CR", "/device\r?user_code=X"],
  ["a NUL", "/device\u0000"],
  ["javascript:", "javascript:alert(1)"],
] as const;

describe("/join/[token] — return_to (#1655)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
  });

  it.each(PROVIDERS)(
    "%s sign-up returns to a relative return_to with the invite",
    async (provider, label) => {
      mockSearchParams.set("return_to", "/device?user_code=ABCD1234");
      expect(await signUpWith(label)).toBe(
        loginUrl(provider, `${FRONTEND_ORIGIN}/device?user_code=ABCD1234`),
      );
    },
  );

  it.each(PROVIDERS)(
    "%s sign-up keeps a same-origin absolute return_to",
    async (provider, label) => {
      const authorize = `${FRONTEND_ORIGIN}/api/v1/oauth/authorize?client_id=c&state=s`;
      mockSearchParams.set("return_to", authorize);
      expect(await signUpWith(label)).toBe(loginUrl(provider, authorize));
    },
  );

  it.each(UNSAFE_RETURN_TO)(
    "falls back to the dashboard for %s",
    async (_label, value) => {
      mockSearchParams.set("return_to", value);
      expect(await signUpWith("join.valid.continueWithGitHub")).toBe(
        loginUrl("github", `${FRONTEND_ORIGIN}/workspace/dashboard`),
      );
      // Still a working invite: no error on the page.
      expect(screen.queryByRole("heading", { name: /error|invalid/ })).toBeNull();
    },
  );
});

describe("/join/[token] — terms of service (#1655)", () => {
  it("keeps both provider buttons disabled until the terms box is ticked", async () => {
    renderPage();
    const google = await screen.findByRole("button", {
      name: "join.valid.continueWithGoogle",
    });
    const github = screen.getByRole("button", {
      name: "join.valid.continueWithGitHub",
    });
    expect(google).toBeDisabled();
    expect(github).toBeDisabled();

    fireEvent.click(google);
    fireEvent.click(github);
    expect(hrefAssignments).toHaveLength(0);

    await agreeToTerms();
    expect(google).toBeEnabled();
    expect(github).toBeEnabled();
  });

  it("shows no terms box when there is no provider to sign up with", async () => {
    mockGetAuthConfig.mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByText("join.valid.noProviders")).toBeVisible();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});

describe("/join/[token] — already signed in, with return_to (#1655)", () => {
  beforeEach(() => {
    mockApiClientGet.mockResolvedValue({ user_id: "u1" });
  });

  async function continueLink() {
    await screen.findByRole("heading", { name: "join.alreadySignedIn.title" });
    return screen.getByRole("link");
  }

  it("continues to a validated relative return_to", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABCD1234");
    renderPage();
    const link = await continueLink();
    expect(link).toHaveAttribute("href", "/device?user_code=ABCD1234");
    expect(link).toHaveTextContent("join.alreadySignedIn.continue");
    expect(mockPreview).not.toHaveBeenCalled();
  });

  it("reduces a same-origin absolute return_to to a path", async () => {
    mockSearchParams.set(
      "return_to",
      `${FRONTEND_ORIGIN}/api/v1/oauth/authorize?client_id=c`,
    );
    renderPage();
    expect(await continueLink()).toHaveAttribute(
      "href",
      "/api/v1/oauth/authorize?client_id=c",
    );
    expect(mockPreview).not.toHaveBeenCalled();
  });

  it.each([
    ...UNSAFE_RETURN_TO,
    ["a same-origin //host pathname", `${FRONTEND_ORIGIN}//evil.example/x`] as const,
  ])("links to the dashboard for %s", async (_label, value) => {
    mockSearchParams.set("return_to", value);
    renderPage();
    const link = await continueLink();
    expect(link).toHaveAttribute("href", "/workspace/dashboard");
    expect(mockPreview).not.toHaveBeenCalled();
  });
});

describe("/join/[token] — back to login keeps return_to (#1655)", () => {
  const STATES = [
    [
      "expired",
      "join.expired.title",
      () =>
        mockPreview.mockRejectedValue(
          new ApiError({ message: "gone", status: 410 }),
        ),
    ],
    [
      "invalid",
      "join.invalid.title",
      () =>
        mockPreview.mockRejectedValue(
          new ApiError({ message: "not found", status: 404 }),
        ),
    ],
    [
      "disabled",
      "join.disabled.title",
      () => {
        mockFeatures = { beta_invites: false };
        mockPreview.mockRejectedValue(
          new ApiError({ message: "not found", status: 404 }),
        );
      },
    ],
    [
      "error",
      "join.error.title",
      () =>
        mockPreview.mockRejectedValue(
          new ApiError({ message: "nope", status: 503 }),
        ),
    ],
  ] as const;

  it.each(STATES)("%s: keeps a validated return_to", async (_s, title, arrange) => {
    arrange();
    mockSearchParams.set("return_to", "/device?user_code=ABCD1234");
    renderPage();
    await screen.findByRole("heading", { name: title });
    expect(
      screen.getByRole("link", { name: "join.backToLogin" }),
    ).toHaveAttribute(
      "href",
      `/login?return_to=${encodeURIComponent("/device?user_code=ABCD1234")}`,
    );
  });

  it.each(STATES)("%s: drops an invalid return_to", async (_s, title, arrange) => {
    arrange();
    mockSearchParams.set("return_to", "https://evil.example/device");
    renderPage();
    await screen.findByRole("heading", { name: title });
    expect(
      screen.getByRole("link", { name: "join.backToLogin" }),
    ).toHaveAttribute("href", "/login");
  });
});

describe("/join/[token] — server-side terms acceptance (#1665)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
  });

  it.each(PROVIDERS)(
    "%s sign-up carries the deployment's terms version",
    async (provider, label) => {
      mockTermsVersion = "2026-09";
      expect(await signUpWith(label)).toBe(
        `${loginUrl(provider, `${FRONTEND_ORIGIN}/workspace/dashboard`)}&accepted_terms=2026-09`,
      );
    },
  );

  it("sends no accepted_terms when the deployment records none", async () => {
    mockTermsVersion = null;
    const url = await signUpWith("join.valid.continueWithGoogle");
    expect(url).toBe(
      loginUrl("google", `${FRONTEND_ORIGIN}/workspace/dashboard`),
    );
    expect(url).not.toContain("accepted_terms");
  });
});
