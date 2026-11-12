/**
 * /login — the `email_in_use` link hint (#2786). Every `email_in_use` gets the
 * same notice: the page cannot tell whether a confirmation mail went out, and
 * does not try. Only a deployment that can send that mail
 * (`features.provider_link_confirmation`) mentions it; otherwise, and while
 * /system/info is unknown, the notice is the plain "sign in, then connect in
 * Profile" hint.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const mockGetAuthConfig = vi.fn();
vi.mock("@/lib/auth/auth", () => ({
  getAuthUrl: vi.fn(),
  getGitHubAuthUrl: vi.fn(),
  getAuthConfig: (...args: unknown[]) => mockGetAuthConfig(...args),
  loginWithPassword: vi.fn(),
  verifyMfa: vi.fn(),
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: null, isLoading: false }),
}));

const mockT = (k: string) => k;
vi.mock("next-intl", () => ({ useTranslations: () => mockT }));

const mockSearchParams = new URLSearchParams();
const mockRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  useSearchParams: () => mockSearchParams,
}));

let mockFeatures: Record<string, boolean> | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
  useSystemInfo: () => ({ features: mockFeatures ?? {}, terms_version: null }),
}));

vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));
vi.mock("@/components/auth/PasskeyLogin", () => ({ default: () => null }));

import LoginPage from "./page";

beforeEach(() => {
  mockFeatures = null;
  for (const key of [...mockSearchParams.keys()]) mockSearchParams.delete(key);
  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: true,
    github_oauth_enabled: true,
  });
});

function emailInUse() {
  mockSearchParams.set("error", "email_in_use");
  mockSearchParams.set("link_hint", "true");
}

describe("/login — email_in_use link hint (#2786)", () => {
  it("mentions the confirmation mail when the deployment can send it", async () => {
    mockFeatures = { provider_link_confirmation: true };
    emailInUse();
    render(<LoginPage />);

    expect(await screen.findByText("emailInUse")).toBeInTheDocument();
    expect(screen.getByText("linkHintConfirmation")).toBeInTheDocument();
    expect(screen.queryByText("linkHint")).toBeNull();
  });

  it.each([
    ["the deployment cannot send it", { provider_link_confirmation: false }],
    ["an older backend does not report it", { beta_invites: true }],
    ["/system/info failed", {}],
  ])("keeps the plain hint when %s", async (_label, features) => {
    mockFeatures = features;
    emailInUse();
    render(<LoginPage />);

    expect(await screen.findByText("emailInUse")).toBeInTheDocument();
    expect(screen.getByText("linkHint")).toBeInTheDocument();
    expect(screen.queryByText("linkHintConfirmation")).toBeNull();
  });

  it("switches to the confirmation wording once /system/info answers", async () => {
    emailInUse();
    const { rerender } = render(<LoginPage />);
    expect(await screen.findByText("linkHint")).toBeInTheDocument();

    mockFeatures = { provider_link_confirmation: true };
    rerender(<LoginPage />);

    expect(await screen.findByText("linkHintConfirmation")).toBeInTheDocument();
    expect(screen.queryByText("linkHint")).toBeNull();
  });

  it("shows no hint for email_in_use without link_hint", async () => {
    mockFeatures = { provider_link_confirmation: true };
    mockSearchParams.set("error", "email_in_use");
    render(<LoginPage />);

    expect(await screen.findByText("emailInUse")).toBeInTheDocument();
    expect(screen.queryByText("linkHint")).toBeNull();
    expect(screen.queryByText("linkHintConfirmation")).toBeNull();
  });

  it("leaves the cancelled-sign-in notice alone", async () => {
    mockFeatures = { provider_link_confirmation: true };
    mockSearchParams.set("cancelled", "1");
    render(<LoginPage />);

    expect(await screen.findByText("signinCancelled")).toBeInTheDocument();
    expect(screen.queryByText("linkHintConfirmation")).toBeNull();
  });
});
