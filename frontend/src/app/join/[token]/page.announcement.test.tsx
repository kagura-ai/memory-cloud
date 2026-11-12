/**
 * /join/[token]: the announcement-email opt-in (#2761).
 *
 * Shown under the terms box only when /system/info names a consent version,
 * unchecked, never gating the buttons; the answer (ticked or not) rides the
 * OAuth login URL after the prefs; without a version nothing is sent.
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

let mockAnnouncementVersion: string | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => ({ beta_invites: true }),
  useSystemInfo: () => ({
    features: { beta_invites: true },
    terms_version: null,
    announcement_consent_version: mockAnnouncementVersion,
  }),
}));

vi.mock("next-intl", () => ({
  useLocale: () => "en",
  useTranslations: () => (k: string) => k,
}));

const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useSearchParams: () => mockSearchParams,
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));

vi.mock("@/lib/auth/signupPrefs", () => ({
  signupPrefs: () => ({ locale: "ja", timezone: "Asia/Tokyo" }),
}));

vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return {
    ...actual,
    use: <T,>(value: T | Promise<T>): T => value as T,
  };
});

import { INVITE_HANDOFF_TEST_IDS as T } from "@/components/auth/invite-handoff.testids";
import JoinPage from "./page";

const TOKEN = "tok_join_announcement_1";
const ORIGIN = "http://localhost:3000";
const originalLocation = window.location;
let hrefAssignments: string[] = [];

function renderPage() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return render(<JoinPage params={{ token: TOKEN } as any} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  hrefAssignments = [];
  mockAnnouncementVersion = null;
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get origin() {
        return ORIGIN;
      },
      get pathname() {
        return `/join/${TOKEN}`;
      },
      get search() {
        return "";
      },
      get href() {
        return `${ORIGIN}/join/${TOKEN}`;
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
});

async function signUp(tick: boolean) {
  renderPage();
  fireEvent.click(
    await screen.findByRole("checkbox", { name: /agreeToTerms/ }),
  );
  if (tick) fireEvent.click(screen.getByTestId(T.joinAnnouncementOptIn));
  fireEvent.click(
    await screen.findByRole("button", {
      name: "join.valid.continueWithGitHub",
    }),
  );
  await waitFor(() => expect(hrefAssignments).toHaveLength(1));
  return new URL(hrefAssignments[0]);
}

describe("/join/[token] — announcement-email opt-in (#2761)", () => {
  it("is not shown without a consent version, and nothing is sent", async () => {
    const url = await signUp(false);
    expect(screen.queryByTestId(T.joinAnnouncementOptIn)).toBeNull();
    expect(url.searchParams.has("announcement_emails")).toBe(false);
  });

  it("is shown unchecked and sends false when left alone", async () => {
    mockAnnouncementVersion = "2026-11";
    renderPage();
    const box = (await screen.findByTestId(
      T.joinAnnouncementOptIn,
    )) as HTMLInputElement;
    expect(box.checked).toBe(false);
    hrefAssignments = [];
    const url = await (async () => {
      fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/ }));
      fireEvent.click(
        screen.getByRole("button", { name: "join.valid.continueWithGitHub" }),
      );
      await waitFor(() => expect(hrefAssignments).toHaveLength(1));
      return new URL(hrefAssignments[0]);
    })();
    expect(url.searchParams.get("announcement_emails")).toBe("false");
    expect(url.searchParams.get("announcement_consent_version")).toBe(
      "2026-11",
    );
    expect(url.searchParams.get("invite")).toBe(TOKEN);
  });

  it("sends true when ticked", async () => {
    mockAnnouncementVersion = "2026-11";
    const url = await signUp(true);
    expect(url.searchParams.get("announcement_emails")).toBe("true");
  });

  it("does not gate the sign-up buttons", async () => {
    mockAnnouncementVersion = "2026-11";
    renderPage();
    fireEvent.click(
      await screen.findByRole("checkbox", { name: /agreeToTerms/ }),
    );
    expect(
      screen.getByRole("button", { name: "join.valid.continueWithGitHub" }),
    ).not.toBeDisabled();
  });
});
