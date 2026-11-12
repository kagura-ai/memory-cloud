/**
 * "Invite team member" dialog copy on the workspace members page (#2787).
 *
 * The page test mocks `useTranslations` to echo the key, so the wording is
 * pinned here: the expiry select is the invitation's (`expires_in_days`), not
 * the membership's, and the address note names no sign-in provider — people
 * join with Google, GitHub or an email sign-up, and what counts is the
 * verified address they sign in with (#2785).
 */
import { describe, expect, it } from "vitest";

import en from "./en.json";
import ja from "./ja.json";

describe("invite dialog messages", () => {
  it("labels the expiry as the invitation's", () => {
    expect(en.workspace.expiresIn).toBe("Invitation expires in");
    expect(ja.workspace.expiresIn).toBe("招待の有効期限");
  });

  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)("%s address note and error name no sign-in provider", (_, m) => {
    for (const text of [
      m.workspace.emailMustMatch,
      m.workspace.emailRequiredError,
    ]) {
      expect(text).toBeTruthy();
      expect(text).not.toMatch(/google|github|oauth/i);
    }
  });

  it("says the address must be the verified one the person signs in with", () => {
    expect(en.workspace.emailMustMatch).toContain("verified address");
    expect(ja.workspace.emailMustMatch).toContain("確認済みのアドレス");
  });

  it("ja names the invited person, not the one who invites", () => {
    for (const text of [
      ja.workspace.emailMustMatch,
      ja.workspace.emailRequiredError,
    ]) {
      expect(text).toContain("招待される人");
      expect(text).not.toContain("招待する人");
    }
  });
});
