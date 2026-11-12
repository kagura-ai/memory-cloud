/**
 * /login: the announcement-email opt-in (#2761).
 *
 * - shown in the OAuth area only when /system/info names an
 *   `announcement_consent_version`, unchecked by default, separate from the
 *   terms checkbox, and never gating the sign-in buttons;
 * - the answer (ticked or not) goes with the version on both the redirect and
 *   the JSON-mode login; without a version nothing is sent.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { AuthProvider } from "@/contexts/AuthContext";
import LoginPage from "./page";

const mockGetAuthConfig = vi.fn();
const mockGetAuthUrl = vi.fn();
const mockGetGitHubAuthUrl = vi.fn();
const mockGetCurrentUser = vi.fn();

vi.mock("@/lib/auth/auth", () => ({
  getAuthUrl: (...args: unknown[]) => mockGetAuthUrl(...args),
  getGitHubAuthUrl: (...args: unknown[]) => mockGetGitHubAuthUrl(...args),
  getAuthConfig: (...args: unknown[]) => mockGetAuthConfig(...args),
  loginWithPassword: vi.fn(),
  verifyMfa: vi.fn(),
  getCurrentUser: (...args: unknown[]) => mockGetCurrentUser(...args),
  logout: vi.fn(),
}));

const mockT = (k: string) => k;
vi.mock("next-intl", () => ({
  useTranslations: () => mockT,
}));

const mockRouter = { push: vi.fn(), replace: vi.fn() };
const mockSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  useSearchParams: () => mockSearchParams,
}));

let mockAnnouncementVersion: string | null = null;
let mockTermsVersion: string | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => ({}),
  useSystemInfo: () => ({
    features: {},
    terms_version: mockTermsVersion,
    announcement_consent_version: mockAnnouncementVersion,
  }),
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));

const ORIGIN = window.location.origin;
const originalLocation = window.location;
let hrefAssignments: string[];

beforeEach(() => {
  mockGetAuthConfig.mockReset();
  mockGetAuthUrl.mockReset();
  mockGetGitHubAuthUrl.mockReset();
  mockGetCurrentUser.mockReset();
  mockGetCurrentUser.mockResolvedValue(null);
  mockAnnouncementVersion = null;
  mockTermsVersion = null;
  for (const key of [...mockSearchParams.keys()]) mockSearchParams.delete(key);
  hrefAssignments = [];
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get origin() {
        return ORIGIN;
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
  cleanup();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  vi.restoreAllMocks();
});

async function renderWithProviders(provider: "google" | "github") {
  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: provider === "google",
    github_oauth_enabled: provider === "github",
  });
  render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
  const name =
    provider === "google" ? /continueWithGoogle/i : /continueWithGitHub/i;
  const button = await screen.findByRole("button", { name });
  fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
  return button;
}

describe("LoginPage announcement-email opt-in (#2761)", () => {
  it("is not shown without a consent version", async () => {
    await renderWithProviders("google");
    expect(screen.queryByTestId("login-announcement-optin")).toBeNull();
  });

  it("is shown unchecked, separate from the terms box", async () => {
    mockAnnouncementVersion = "2026-11";
    await renderWithProviders("google");

    const box = screen.getByTestId(
      "login-announcement-optin",
    ) as HTMLInputElement;
    expect(box.checked).toBe(false);
    const terms = screen.getByRole("checkbox", { name: /agreeToTerms/i });
    expect(terms).not.toBe(box);
    expect(screen.getByRole("checkbox", { name: /optIn/i })).toBe(box);
  });

  it("sends an unchecked box as false on the redirect", async () => {
    mockAnnouncementVersion = "2026-11";
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    const button = await renderWithProviders("github");

    expect(button).not.toBeDisabled();
    fireEvent.click(button);

    await waitFor(() => expect(hrefAssignments.length).toBeGreaterThan(0));
    const url = new URL(hrefAssignments[0]);
    expect(url.searchParams.get("announcement_emails")).toBe("false");
    expect(url.searchParams.get("announcement_consent_version")).toBe(
      "2026-11",
    );
  });

  it("sends a ticked box as true to the JSON-mode login", async () => {
    mockAnnouncementVersion = "2026-11";
    mockTermsVersion = "2026-09";
    mockGetAuthUrl.mockResolvedValue("https://accounts.google.com/oauth/auth");
    const button = await renderWithProviders("google");

    fireEvent.click(screen.getByTestId("login-announcement-optin"));
    fireEvent.click(button);

    await waitFor(() => expect(mockGetAuthUrl).toHaveBeenCalledTimes(1));
    expect(mockGetAuthUrl).toHaveBeenCalledWith("2026-09", {
      value: true,
      version: "2026-11",
    });
  });

  it("sends nothing without a consent version", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABC");
    const button = await renderWithProviders("google");
    fireEvent.click(button);

    await waitFor(() => expect(hrefAssignments.length).toBeGreaterThan(0));
    expect(hrefAssignments[0]).not.toContain("announcement_");
  });
});
