/**
 * The plan page's notice about the application (#2950), read from the
 * catalogues themselves: component tests echo keys, so only this file sees the
 * words a reader gets.
 *
 * Anyone can link to the arrival URL, and the page knows nothing about the
 * reader's past, so no text may state as a fact where the reader came from,
 * that something is waiting or needed, or that a payment happened. The heading
 * and the hint are conditional; the link label names where the link goes.
 */
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import en from "./en.json";
import ja from "./ja.json";

const KEYS = [
  "title",
  "signedInAs",
  "notOnPlan",
  "onPlan",
  "switchHint",
  "returnLink",
  "checkAgain",
  "dismiss",
  "nowOnPlan",
] as const;

const FORBIDDEN = {
  en: [
    /\bpa(id|y|yment|ying)\b/i,
    /\bpurchas/i,
    /\bbought\b/i,
    /\bbuy/i,
    /\bcheckout\b/i,
    /\bsubscri/i,
    /\bwait/i,
    /\bpending\b/i,
    /\bneed/i,
    /\brequire/i,
    /\byou came\b/i,
    /\bcame from\b/i,
    /\bsent you\b/i,
    /\bredirect/i,
    /\bcancel/i,
    /\bsucce/i,
    /\bcomplete/i,
    /\bwelcome back\b/i,
  ],
  ja: [
    /支払/,
    /決済/,
    /購入/,
    /課金/,
    /契約/,
    /待/,
    /必要/,
    /来ました/,
    /戻ってきた/,
    /お帰り/,
    /リダイレクト/,
    /キャンセル/,
    /完了/,
    /成功/,
  ],
} as const;

const CATALOGUES = [
  ["en", en.workspace.planPage.applicationReturn],
  ["ja", ja.workspace.planPage.applicationReturn],
] as const;

describe("application-return texts (#2950)", () => {
  it.each(CATALOGUES)("%s has exactly the nine keys", (_locale, texts) => {
    expect(Object.keys(texts).sort()).toEqual([...KEYS].sort());
    for (const key of KEYS) expect(texts[key].trim()).not.toBe("");
  });

  it("no en or ja application-return text states a source, a wait or a payment", () => {
    for (const [locale, texts] of CATALOGUES) {
      for (const key of KEYS) {
        for (const pattern of FORBIDDEN[locale]) {
          expect({ locale, key, pattern: String(pattern), match: pattern.test(texts[key]) }).toEqual({
            locale,
            key,
            pattern: String(pattern),
            match: false,
          });
        }
      }
    }
    // The heading and the hint are conditional, never a statement of fact.
    const [[, enTexts], [, jaTexts]] = CATALOGUES;
    expect(enTexts.title).toMatch(/^If /);
    expect(enTexts.switchHint).toMatch(/^If /);
    expect(jaTexts.title).toMatch(/場合$/);
    expect(jaTexts.switchHint).toMatch(/場合は/);
  });

  it.each(CATALOGUES)("%s formats with the arguments the notice passes", (locale, texts) => {
    const t = createTranslator({
      locale,
      messages: { applicationReturn: texts },
      namespace: "applicationReturn",
      onError: (error) => {
        throw error;
      },
    });
    const account = "owner@example.com";
    expect(t("signedInAs", { account })).toMatch(/owner@example\.com/);
    const stated = t("notOnPlan", { workspace: "Research", plan: "Max" });
    expect(stated).toMatch(/Research/);
    expect(stated).toMatch(/Max/);
    expect(t("onPlan", { workspace: "Research", plan: "Max" })).toMatch(/Research/);
    expect(t("nowOnPlan", { plan: "Max" })).toMatch(/Max/);
  });
});
