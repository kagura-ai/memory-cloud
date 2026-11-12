/**
 * /join/[token] — email sign-up (#1734): with `features.email_signup` the
 * valid invite also offers the email form, carrying the invite token and the
 * validated return_to; without any OAuth provider the form replaces the
 * "no sign-up method" text.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const mockPreview = vi.fn();
const mockGetAuthConfig = vi.fn();
vi.mock("@/lib/api/beta-invites", () => ({
  previewBetaInvite: (...args: unknown[]) => mockPreview(...args),
}));
vi.mock("@/lib/api/base", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/base")>("@/lib/api/base");
  return {
    ...actual,
    apiClient: { get: () => Promise.reject(new Error("Not authenticated")) },
  };
});
vi.mock("@/lib/auth/auth", () => ({
  getAuthConfig: () => mockGetAuthConfig(),
}));

let mockFeatures: Record<string, boolean> = {};
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
  useSystemInfo: () => ({ features: mockFeatures, terms_version: null }),
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
vi.mock("@/components/common/LoadingState", () => ({
  SpinnerLoading: ({ message }: { message?: string }) => (
    <div role="status">{message}</div>
  ),
}));
vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return { ...actual, use: <T,>(value: T | Promise<T>): T => value as T };
});
vi.mock("@/components/auth/EmailSignupForm", () => ({
  EmailSignupForm: (props: {
    returnTo?: string;
    defaultOpen?: boolean;
    invite?: string;
    themed?: boolean;
  }) => (
    <div
      data-testid="email-signup-form"
      data-return-to={props.returnTo ?? ""}
      data-default-open={String(Boolean(props.defaultOpen))}
      data-invite={props.invite ?? ""}
      data-themed={String(Boolean(props.themed))}
    />
  ),
}));

import JoinPage from "./page";

const TOKEN = "tok_join_abcdefghijklmnop";

function renderPage() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return render(<JoinPage params={{ token: TOKEN } as any} />);
}

beforeEach(() => {
  mockFeatures = { beta_invites: true, email_signup: true };
  for (const key of [...mockSearchParams.keys()]) mockSearchParams.delete(key);
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

describe("/join/[token] — email sign-up (#1734)", () => {
  it("offers the form below the providers, with the invite and return_to", async () => {
    mockSearchParams.set("return_to", "/device?user_code=ABCD");
    renderPage();
    const form = await screen.findByTestId("email-signup-form");
    expect(form).toHaveAttribute("data-invite", TOKEN);
    expect(form).toHaveAttribute("data-return-to", "/device?user_code=ABCD");
    expect(form).toHaveAttribute("data-default-open", "false");
    expect(form).toHaveAttribute("data-themed", "true");
    expect(screen.getByText("request.divider")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "join.valid.continueWithGoogle" }),
    ).toBeVisible();
  });

  it("replaces the no-provider text with an open form", async () => {
    mockGetAuthConfig.mockResolvedValue({
      password_login_enabled: true,
      google_oauth_enabled: false,
      github_oauth_enabled: false,
    });
    renderPage();
    const form = await screen.findByTestId("email-signup-form");
    expect(form).toHaveAttribute("data-default-open", "true");
    expect(screen.queryByText("join.valid.noProviders")).toBeNull();
    expect(screen.queryByText("request.divider")).toBeNull();
  });

  it("is absent when email sign-up is off", async () => {
    mockFeatures = { beta_invites: true };
    renderPage();
    await screen.findByRole("heading", { name: "join.valid.title" });
    expect(screen.queryByTestId("email-signup-form")).toBeNull();
  });
});
