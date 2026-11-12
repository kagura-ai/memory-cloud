/**
 * Email sign-up completion carries the announcement-email opt-in (#1734 +
 * #2761): the answer and the wording version it was shown with become
 * `announcement_emails` / `announcement_consent_version`; without an answer
 * neither field is sent.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockPost = vi.fn();
vi.mock("@/lib/api/base", () => ({
  apiClient: { post: (...args: unknown[]) => mockPost(...args) },
}));
vi.mock("@/lib/auth/signupPrefs", () => ({
  signupPrefs: () => ({ locale: "ja", timezone: "Asia/Tokyo" }),
}));

import { completeEmailSignup } from "./emailSignup";

beforeEach(() => {
  mockPost.mockReset();
  mockPost.mockResolvedValue({ redirect_url: "/" });
});

describe("completeEmailSignup — announcement opt-in", () => {
  it.each([true, false])(
    "sends the answer %s with its version",
    async (value) => {
      await completeEmailSignup({
        token: "t",
        newPassword: "pw",
        announcementEmails: { value, version: "2026-11" },
      });
      expect(mockPost).toHaveBeenCalledWith(
        "/api/v1/auth/signup/email/complete",
        {
          token: "t",
          new_password: "pw",
          announcement_emails: value,
          announcement_consent_version: "2026-11",
        },
      );
    },
  );

  it("sends neither field without an answer", async () => {
    await completeEmailSignup({ token: "t", newPassword: "pw" });
    const body = mockPost.mock.calls[0][1] as Record<string, unknown>;
    expect(body).not.toHaveProperty("announcement_emails");
    expect(body).not.toHaveProperty("announcement_consent_version");
  });
});
