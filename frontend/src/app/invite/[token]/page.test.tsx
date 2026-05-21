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
