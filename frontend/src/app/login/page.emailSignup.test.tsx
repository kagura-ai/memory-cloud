/**
 * /login — "Create an account with email" (#1734): shown only when the
 * deployment has email sign-up on AND registration open, and hands the
 * validated return_to to the form (which keeps it in this browser).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

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

// The form has its own tests; here only what the page hands it matters.
vi.mock("@/components/auth/EmailSignupForm", () => ({
  EmailSignupForm: (props: {
    returnTo?: string;
    defaultOpen?: boolean;
    invite?: string;
  }) => (
    <div
      data-testid="email-signup-form"
      data-return-to={props.returnTo ?? ""}
      data-default-open={String(Boolean(props.defaultOpen))}
      data-invite={props.invite ?? ""}
    />
  ),
}));

import LoginPage from "./page";

beforeEach(() => {
  mockFeatures = null;
  for (const key of [...mockSearchParams.keys()]) mockSearchParams.delete(key);
  mockGetAuthConfig.mockResolvedValue({
    password_login_enabled: true,
    google_oauth_enabled: true,
    github_oauth_enabled: false,
  });
});

describe("/login — email sign-up entry (#1734)", () => {
  it("opens the form with the validated return_to", async () => {
    mockFeatures = { email_signup: true, open_registration: true };
    mockSearchParams.set("return_to", "/device?user_code=ABCD");
    render(<LoginPage />);

    fireEvent.click(await screen.findByRole("button", { name: "signUpWithEmail" }));
    const form = screen.getByTestId("email-signup-form");
    expect(form).toHaveAttribute("data-return-to", "/device?user_code=ABCD");
    expect(form).toHaveAttribute("data-default-open", "true");
    expect(form).toHaveAttribute("data-invite", "");
  });

  it("drops an unsafe return_to", async () => {
    mockFeatures = { email_signup: true, open_registration: true };
    mockSearchParams.set("return_to", "//evil.example");
    render(<LoginPage />);

    fireEvent.click(await screen.findByRole("button", { name: "signUpWithEmail" }));
    expect(screen.getByTestId("email-signup-form")).toHaveAttribute(
      "data-return-to",
      "",
    );
  });

  it.each([
    ["registration is closed", { email_signup: true, open_registration: false }],
    ["email sign-up is off", { email_signup: false, open_registration: true }],
    ["an older backend sends neither flag", { beta_invites: true }],
  ])("is hidden when %s", async (_label, features) => {
    mockFeatures = features;
    render(<LoginPage />);
    await screen.findByRole("button", { name: /continueWithGoogle/ });
    expect(
      screen.queryByRole("button", { name: "signUpWithEmail" }),
    ).toBeNull();
    expect(screen.queryByTestId("email-signup-form")).toBeNull();
  });

  it("shows method-neutral copy for email_in_use with the link hint", async () => {
    mockSearchParams.set("error", "email_in_use");
    mockSearchParams.set("link_hint", "true");
    render(<LoginPage />);
    expect(await screen.findByText("emailInUse")).toBeInTheDocument();
    expect(screen.getByText("linkHint")).toBeInTheDocument();
  });
});
