import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const mockGetInvitationInfo = vi.fn();
const mockAcceptInvitation = vi.fn();
const mockApiClientGet = vi.fn();

vi.mock("@/lib/api/invitations", () => ({
  getInvitationInfo: (...args: unknown[]) => mockGetInvitationInfo(...args),
  acceptInvitation: (...args: unknown[]) => mockAcceptInvitation(...args),
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

// #1665: the terms version /system/info reports; null = not recorded.
let mockTermsVersion: string | null = null;
// #1665: true = /system/info has not answered yet (the hook returns null).
let mockSystemInfoPending = false;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () =>
    mockSystemInfoPending
      ? null
      : { features: {}, terms_version: mockTermsVersion },
}));

// #1665: the re-acceptance dialog posts through this.
const mockAcceptTerms = vi.fn();
vi.mock("@/lib/auth/auth", () => ({
  acceptTerms: (...args: unknown[]) => mockAcceptTerms(...args),
}));

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));

vi.mock("@/components/common/LoadingState", () => ({
  SpinnerLoading: () => null,
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

import AcceptInvitationPage from "./page";

const TOKEN = "test-invitation-token";
const originalLocation = window.location;
const FRONTEND_ORIGIN = "http://localhost:3000";
const INVITE_URL = `${FRONTEND_ORIGIN}/invite/${TOKEN}`;

let hrefAssignments: string[] = [];

beforeEach(() => {
  mockGetInvitationInfo.mockReset();
  mockAcceptInvitation.mockReset();
  mockApiClientGet.mockReset();
  mockPush.mockReset();
  hrefAssignments = [];
  mockTermsVersion = null;
  mockSystemInfoPending = false;
  mockAcceptTerms.mockReset();

  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get origin() {
        return FRONTEND_ORIGIN;
      },
      get pathname() {
        return `/invite/${TOKEN}`;
      },
      get search() {
        return "";
      },
      get href() {
        return hrefAssignments.length > 0
          ? hrefAssignments[hrefAssignments.length - 1]
          : INVITE_URL;
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
  // Note: per-test `vi.stubEnv("NEXT_PUBLIC_API_URL", ...)` is intentionally
  // called *after* render. Safe today because buildOAuthRedirect reads
  // process.env at click time, not render time. If the helper ever memoizes
  // its URL at render, move the stub into beforeEach.
});

afterEach(() => {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  vi.unstubAllEnvs();
});

async function renderInLoginRequiredState(): Promise<void> {
  // params type is Promise<{token}>; the react.use mock above unwraps plain values.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  render(<AcceptInvitationPage params={{ token: TOKEN } as any} />);
  await screen.findByRole("button", { name: /loginButton/i });
}

describe("AcceptInvitationPage OAuth login wiring", () => {
  it("renders both Google and GitHub OAuth buttons in login_required state", async () => {
    await renderInLoginRequiredState();

    expect(screen.getByRole("button", { name: /loginButton/i })).toBeDefined();
    expect(
      screen.getByRole("button", { name: /continueWithGitHub/i }),
    ).toBeDefined();
  });

  it("Google button navigates to /auth/google/login with absolute same-origin return_to", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    await renderInLoginRequiredState();

    fireEvent.click(screen.getByRole("button", { name: /loginButton/i }));

    await waitFor(() => {
      expect(hrefAssignments.length).toBeGreaterThan(0);
    });
    const navigated = hrefAssignments[hrefAssignments.length - 1];
    const expectedReturnTo = encodeURIComponent(INVITE_URL);
    expect(navigated).toBe(
      `https://api.example.com/api/v1/auth/google/login?return_to=${expectedReturnTo}`,
    );
    expect(navigated).not.toMatch(/\/api\/v1\/api\/v1\//);
  });

  it("GitHub button navigates to /auth/github/login with absolute same-origin return_to", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    await renderInLoginRequiredState();

    fireEvent.click(
      screen.getByRole("button", { name: /continueWithGitHub/i }),
    );

    await waitFor(() => {
      expect(hrefAssignments.length).toBeGreaterThan(0);
    });
    const navigated = hrefAssignments[hrefAssignments.length - 1];
    const expectedReturnTo = encodeURIComponent(INVITE_URL);
    expect(navigated).toBe(
      `https://api.example.com/api/v1/auth/github/login?return_to=${expectedReturnTo}`,
    );
    expect(navigated).not.toMatch(/\/api\/v1\/api\/v1\//);
  });

  it("strips a trailing /api/v1 from NEXT_PUBLIC_API_URL", async () => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com/api/v1");
    await renderInLoginRequiredState();

    fireEvent.click(screen.getByRole("button", { name: /loginButton/i }));

    await waitFor(() => {
      expect(hrefAssignments.length).toBeGreaterThan(0);
    });
    expect(hrefAssignments[0]).toMatch(
      /^https:\/\/api\.example\.com\/api\/v1\/auth\/google\/login\?return_to=/,
    );
    expect(hrefAssignments[0]).not.toMatch(/\/api\/v1\/api\/v1\//);
  });
});

describe("AcceptInvitationPage terms acceptance (#1665)", () => {
  it("shows no terms checkbox when the deployment records none", async () => {
    await renderInLoginRequiredState();

    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(
      screen.getByRole("button", { name: /loginButton/i }),
    ).not.toBeDisabled();
  });

  it("asks for the terms and sends the version once ticked", async () => {
    mockTermsVersion = "2026-09";
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.example.com");
    await renderInLoginRequiredState();

    const google = screen.getByRole("button", { name: /loginButton/i });
    const github = screen.getByRole("button", { name: /continueWithGitHub/i });
    expect(google).toBeDisabled();
    expect(github).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    expect(google).not.toBeDisabled();
    fireEvent.click(github);

    await waitFor(() => expect(hrefAssignments).toHaveLength(1));
    expect(hrefAssignments[0]).toBe(
      `https://api.example.com/api/v1/auth/github/login?return_to=${encodeURIComponent(INVITE_URL)}&accepted_terms=2026-09`,
    );
  });
});

describe("AcceptInvitationPage waits for /system/info (#1665)", () => {
  it("keeps the login buttons disabled until it answers", async () => {
    mockSystemInfoPending = true;
    await renderInLoginRequiredState();

    const google = screen.getByRole("button", { name: /loginButton/i });
    expect(google).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /continueWithGitHub/i }),
    ).toBeDisabled();
    fireEvent.click(google);
    expect(hrefAssignments).toHaveLength(0);
  });
});

describe("AcceptInvitationPage signed in with terms to re-accept (#1665)", () => {
  const ME = {
    user: {
      id: "u1",
      email: "u@example.test",
      terms_acceptance_required: true,
      terms_version: "2026-09",
    },
  };

  it("asks for the updated terms before accepting the invitation", async () => {
    mockApiClientGet.mockResolvedValue(ME);
    mockAcceptTerms.mockResolvedValue({
      version: "2026-09",
      recorded: true,
      terms_acceptance_required: false,
    });
    mockAcceptInvitation.mockResolvedValue({
      workspace_name: "Test Workspace",
    });
    // params type is Promise<{token}>; the react.use mock above unwraps plain values.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    render(<AcceptInvitationPage params={{ token: TOKEN } as any} />);

    await screen.findByRole("dialog");
    // Nothing is accepted on the user's behalf while the dialog is up.
    expect(mockAcceptInvitation).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/i }));
    fireEvent.click(screen.getByRole("button", { name: "accept" }));

    await waitFor(() =>
      expect(mockAcceptInvitation).toHaveBeenCalledWith(TOKEN),
    );
    expect(mockAcceptTerms).toHaveBeenCalledWith("2026-09");
  });

  it("accepts straight away when no re-acceptance is required", async () => {
    mockApiClientGet.mockResolvedValue({
      user: { ...ME.user, terms_acceptance_required: false },
    });
    mockAcceptInvitation.mockResolvedValue({
      workspace_name: "Test Workspace",
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    render(<AcceptInvitationPage params={{ token: TOKEN } as any} />);

    await waitFor(() =>
      expect(mockAcceptInvitation).toHaveBeenCalledWith(TOKEN),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
