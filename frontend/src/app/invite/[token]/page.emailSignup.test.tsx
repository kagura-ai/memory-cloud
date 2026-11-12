/**
 * /invite/[token] — email sign-up and password sign-in (#1734).
 *
 * The login prompt always links to the password sign-in with this page as
 * return_to. The email form appears only when the public preview says the
 * addressee can sign up by email (`email_signup_available`): a link-only
 * invitation, or one the sign-up controls would refuse, must not lead to a
 * form that silently sends nothing.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import ja from "@/messages/ja.json";

const mockGetInvitationInfo = vi.fn();
vi.mock("@/lib/api/invitations", () => ({
  getInvitationInfo: (...args: unknown[]) => mockGetInvitationInfo(...args),
  acceptInvitation: vi.fn(),
}));
vi.mock("@/lib/api/base", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/base")>("@/lib/api/base");
  return {
    ...actual,
    apiClient: { get: () => Promise.reject(new Error("Not authenticated")) },
  };
});
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => ({ features: {}, terms_version: null }),
}));
vi.mock("@/lib/auth/auth", () => ({ acceptTerms: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: () => (k: string) => k }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/components/LanguageSelector", () => ({
  LanguageSelector: () => null,
}));
vi.mock("@/components/common/LoadingState", () => ({
  SpinnerLoading: () => null,
}));
vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return { ...actual, use: <T,>(value: T | Promise<T>): T => value as T };
});
vi.mock("@/components/auth/EmailSignupForm", () => ({
  EmailSignupForm: (props: { returnTo?: string; invite?: string }) => (
    <div
      data-testid="email-signup-form"
      data-return-to={props.returnTo ?? ""}
      data-invite={props.invite ?? ""}
    />
  ),
}));

import AcceptInvitationPage from "./page";

const TOKEN = "invitation-token-1";

async function renderLoginRequired() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  render(<AcceptInvitationPage params={{ token: TOKEN } as any} />);
  await screen.findByRole("button", { name: /loginButton/i });
}

beforeEach(() => {
  mockGetInvitationInfo.mockReset();
});

describe("/invite/[token] — email sign-up (#1734)", () => {
  it("offers the form when the preview says email sign-up can work here", async () => {
    mockGetInvitationInfo.mockResolvedValue({
      workspace_name: "Test Workspace",
      email_restricted: true,
      email_signup_available: true,
    });
    await renderLoginRequired();
    const form = screen.getByTestId("email-signup-form");
    // Comes back here to accept; the invitation token is never a beta invite.
    expect(form).toHaveAttribute("data-return-to", `/invite/${TOKEN}`);
    expect(form).toHaveAttribute("data-invite", "");
  });

  it.each([
    ["the preview says no", false],
    ["an older backend sends no field", undefined],
  ])("hides the form when %s", async (_label, available) => {
    mockGetInvitationInfo.mockResolvedValue({
      workspace_name: "Test Workspace",
      email_restricted: false,
      ...(available === undefined
        ? {}
        : { email_signup_available: available }),
    });
    await renderLoginRequired();
    expect(screen.queryByTestId("email-signup-form")).toBeNull();
  });

  it("always links to the password sign-in, open and back to this invitation", async () => {
    mockGetInvitationInfo.mockResolvedValue({
      workspace_name: "Test Workspace",
      email_restricted: false,
    });
    await renderLoginRequired();
    expect(
      screen.getByRole("link", { name: "loginRequired.passwordSignIn" }),
    ).toHaveAttribute(
      "href",
      `/login?method=password&return_to=${encodeURIComponent(`/invite/${TOKEN}`)}`,
    );
  });
});

describe("/invite/[token] Japanese sign-in wording", () => {
  it("says ログイン throughout the sign-in card, as its existing strings do", () => {
    const card = ja.invitation.loginRequired;
    for (const text of Object.values(card)) {
      expect(text).not.toContain("サインイン");
    }
    expect(card.passwordSignIn).toBe("メールアドレスとパスワードでログイン");
  });
});
