/**
 * /invite/[token] (signed out): the announcement-email opt-in (#2761).
 *
 * Shown only when /system/info names a consent version, unchecked, never
 * gating the sign-in buttons; the answer (ticked or not) rides the OAuth login
 * URL; without a version nothing is sent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockGetInvitationInfo = vi.fn();
const mockApiClientGet = vi.fn();

vi.mock("@/lib/api/invitations", () => ({
  getInvitationInfo: (...args: unknown[]) => mockGetInvitationInfo(...args),
  acceptInvitation: vi.fn(),
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

let mockAnnouncementVersion: string | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => ({
    features: {},
    terms_version: null,
    announcement_consent_version: mockAnnouncementVersion,
  }),
}));

vi.mock("@/lib/auth/auth", () => ({
  acceptTerms: vi.fn(),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));

vi.mock("@/lib/auth/signupPrefs", () => ({
  signupPrefs: () => ({ locale: "ja", timezone: "Asia/Tokyo" }),
}));

vi.mock("@/components/common/LoadingState", () => ({
  SpinnerLoading: () => null,
}));

vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return {
    ...actual,
    use: <T,>(value: T | Promise<T>): T => value as T,
  };
});

import { INVITE_HANDOFF_TEST_IDS as T } from "@/components/auth/invite-handoff.testids";
import AcceptInvitationPage from "./page";

const TOKEN = "test-invitation-token";
const ORIGIN = "http://localhost:3000";
const originalLocation = window.location;
let hrefAssignments: string[] = [];

beforeEach(() => {
  mockGetInvitationInfo.mockReset();
  mockApiClientGet.mockReset();
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
        return `/invite/${TOKEN}`;
      },
      get search() {
        return "";
      },
      get href() {
        return (
          hrefAssignments[hrefAssignments.length - 1] ??
          `${ORIGIN}/invite/${TOKEN}`
        );
      },
      set href(val: string) {
        hrefAssignments.push(val);
      },
    },
  });
  mockGetInvitationInfo.mockResolvedValue({
    workspace_name: "Test Workspace",
    email_restricted: false,
  });
  mockApiClientGet.mockRejectedValue(new Error("Not authenticated"));
});

afterEach(() => {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  vi.unstubAllEnvs();
});

async function renderSignedOut() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  render(<AcceptInvitationPage params={{ token: TOKEN } as any} />);
  return screen.findByRole("button", { name: /loginButton/i });
}

describe("AcceptInvitationPage announcement-email opt-in (#2761)", () => {
  it("is not shown without a consent version, and nothing is sent", async () => {
    const button = await renderSignedOut();
    expect(screen.queryByTestId(T.inviteAnnouncementOptIn)).toBeNull();
    fireEvent.click(button);
    await waitFor(() => expect(hrefAssignments.length).toBeGreaterThan(0));
    expect(hrefAssignments[0]).not.toContain("announcement_");
  });

  it("is shown unchecked, does not gate the buttons and sends false", async () => {
    mockAnnouncementVersion = "2026-11";
    const button = await renderSignedOut();
    const box = screen.getByTestId(
      T.inviteAnnouncementOptIn,
    ) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(button).not.toBeDisabled();

    fireEvent.click(button);
    await waitFor(() => expect(hrefAssignments.length).toBeGreaterThan(0));
    const url = new URL(hrefAssignments[0]);
    expect(url.searchParams.get("announcement_emails")).toBe("false");
    expect(url.searchParams.get("announcement_consent_version")).toBe(
      "2026-11",
    );
  });

  it("sends true when ticked", async () => {
    mockAnnouncementVersion = "2026-11";
    const button = await renderSignedOut();
    fireEvent.click(screen.getByTestId(T.inviteAnnouncementOptIn));
    fireEvent.click(button);
    await waitFor(() => expect(hrefAssignments.length).toBeGreaterThan(0));
    expect(
      new URL(hrefAssignments[0]).searchParams.get("announcement_emails"),
    ).toBe("true");
  });
});
