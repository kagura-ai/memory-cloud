/**
 * /signup/verify: the announcement-email opt-in (#1734 + #2761).
 *
 * Shown, unchecked, only while /system/info names a consent version (with or
 * without terms); it never gates the submit, and the answer (ticked or not)
 * goes to `complete` with that version. Without a version there is no box and
 * no answer.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length > 0 ? `${k}:${JSON.stringify(vars)}` : k,
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => ({
    get: (k: string) => (k === "token" ? "tok_announcement" : null),
  }),
  useRouter: () => ({ replace: vi.fn() }),
  usePathname: () => "/signup/verify",
}));

let mockTermsVersion: string | null = null;
let mockAnnouncementVersion: string | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemInfo: () => ({
    features: {},
    terms_version: mockTermsVersion,
    announcement_consent_version: mockAnnouncementVersion,
  }),
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

const { mockComplete } = vi.hoisted(() => ({ mockComplete: vi.fn() }));
vi.mock("@/lib/auth/emailSignup", () => ({
  previewEmailSignup: () =>
    Promise.resolve({
      email: "new@example.com",
      expires_at: "2030-01-01T00:00:00Z",
    }),
  completeEmailSignup: (...a: unknown[]) => mockComplete(...a),
  peekSignupReturnTo: () => undefined,
  clearSignupReturnTo: () => undefined,
}));

import SignupVerifyPage from "./page";

const GOOD = "New-Password-456!";

async function renderForm() {
  render(<SignupVerifyPage />);
  await screen.findByText(
    `verify.description:${JSON.stringify({ email: "new@example.com" })}`,
  );
  fireEvent.change(screen.getByLabelText("newPassword"), {
    target: { value: GOOD },
  });
  fireEvent.change(screen.getByLabelText("confirmPassword"), {
    target: { value: GOOD },
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "verify.submit" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mockTermsVersion = null;
  mockAnnouncementVersion = null;
  // The page navigates with window.location.href after success.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { origin: "http://localhost:3000", href: "" },
  });
  mockComplete.mockResolvedValue({ redirect_url: "/workspace/dashboard" });
});

describe("/signup/verify — announcement opt-in", () => {
  it("no consent version → no box and no answer", async () => {
    mockTermsVersion = "2026-01";
    await renderForm();
    expect(screen.queryByRole("checkbox", { name: "optIn" })).toBeNull();

    fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/ }));
    submit();
    await waitFor(() => expect(mockComplete).toHaveBeenCalledTimes(1));
    expect(mockComplete.mock.calls[0][0].announcementEmails).toBeUndefined();
  });

  it.each([null, "2026-01"])(
    "unchecked by default, never gates the submit, and the 'no' is sent (terms %s)",
    async (terms) => {
      mockTermsVersion = terms;
      mockAnnouncementVersion = "2026-11";
      await renderForm();
      const box = screen.getByRole("checkbox", { name: "optIn" });
      expect(box).not.toBeChecked();
      if (terms) {
        fireEvent.click(screen.getByRole("checkbox", { name: /agreeToTerms/ }));
      }
      expect(
        screen.getByRole("button", { name: "verify.submit" }),
      ).toBeEnabled();

      submit();
      await waitFor(() => expect(mockComplete).toHaveBeenCalledTimes(1));
      expect(mockComplete.mock.calls[0][0].announcementEmails).toEqual({
        value: false,
        version: "2026-11",
      });
    },
  );

  it("a ticked box sends yes with the version", async () => {
    mockAnnouncementVersion = "2026-11";
    await renderForm();
    fireEvent.click(screen.getByRole("checkbox", { name: "optIn" }));
    submit();
    await waitFor(() => expect(mockComplete).toHaveBeenCalledTimes(1));
    expect(mockComplete.mock.calls[0][0].announcementEmails).toEqual({
      value: true,
      version: "2026-11",
    });
  });
});
