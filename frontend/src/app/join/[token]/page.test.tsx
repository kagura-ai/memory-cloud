/**
 * /join/[token] — the beta invite landing page (#1582).
 *
 * Five outcomes from two probes (session, preview) plus the feature flag, and
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
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

vi.mock("next-intl", () => ({
  useLocale: () => "en",
  useTranslations: () => (k: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length > 0 ? `${k}:${JSON.stringify(vars)}` : k,
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
      fireEvent.click(await screen.findByRole("button", { name: label }));

      await waitFor(() => expect(hrefAssignments).toHaveLength(1));
      expect(hrefAssignments[0]).toBe(
        `https://api.example.com/api/v1/auth/${provider}/login` +
          `?return_to=${encodeURIComponent(`${FRONTEND_ORIGIN}/`)}` +
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

  it("a network failure with the feature on → invalid, not a blank page", async () => {
    mockPreview.mockRejectedValue(
      new ApiError({ message: "Network error", status: 0 }),
    );
    renderPage();
    expect(
      await screen.findByRole("heading", { name: "join.invalid.title" }),
    ).toBeVisible();
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
