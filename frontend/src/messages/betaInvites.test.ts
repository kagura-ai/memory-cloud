/**
 * `betaInvites` messages through the real ICU parser (#1582).
 *
 * Component tests mock `useTranslations` to echo the key, so a message that
 * takes an argument is never formatted there, and `icu.test.ts` formats with
 * NO arguments (it only catches malformed patterns). This formats the ones the
 * UI calls with values, and pins en/ja key parity for the namespace.
 */
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import en from "./en.json";
import ja from "./ja.json";

function keys(node: unknown, path: string[] = []): string[] {
  if (node && typeof node === "object") {
    return Object.entries(node as Record<string, unknown>).flatMap(([k, v]) =>
      keys(v, [...path, k]),
    );
  }
  return [path.join(".")];
}

describe("betaInvites messages", () => {
  it("has the same keys in en and ja", () => {
    expect(keys(ja.betaInvites).sort()).toEqual(keys(en.betaInvites).sort());
    expect(ja.signupBlocked.haveInvite).toBeTruthy();
    expect(en.signupBlocked.haveInvite).toBeTruthy();
  });

  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)(
    "%s formats every message the UI passes values to",
    (locale, messages) => {
      const t = createTranslator({
        locale,
        messages,
        namespace: "betaInvites",
        onError: (error) => {
          throw error;
        },
      });

      expect(t("menuEntryWithCount", { used: 4, quota: 4 })).toContain("4/4");
      expect(t("dialog.usage", { used: 1, quota: 4 })).toMatch(/1.*4/);
      expect(t("dialog.usageUnlimited", { used: 9 })).toContain("9");
      expect(t("dialog.capReached", { quota: 4 })).toContain("4");
      for (const key of [
        "dialog.list.expiresAt",
        "dialog.list.expiredAt",
        "dialog.list.redeemedAt",
        "dialog.list.revokedAt",
        "join.valid.expires",
      ] as const) {
        expect(t(key, { date: "2030/01/08" })).toContain("2030/01/08");
      }
      // The plural must resolve for both branches, not just parse.
      expect(t("card.remaining", { remaining: 1 })).toContain("1");
      expect(t("card.remaining", { remaining: 3 })).toContain("3");
    },
  );

  it("pluralises the English remaining count", () => {
    const t = createTranslator({
      locale: "en",
      messages: en,
      namespace: "betaInvites",
    });
    expect(t("card.remaining", { remaining: 1 })).toBe("1 invite left");
    expect(t("card.remaining", { remaining: 3 })).toBe("3 invites left");
  });
});
