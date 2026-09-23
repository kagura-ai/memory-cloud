/**
 * "Coming soon" labels (#1646).
 *
 * The Sidebar renders `t("comingSoon")` (a disabled entry's tooltip) and
 * `t("soon")` (its pill) on the `sidebar` namespace, and the Sidebar's own
 * test mocks `useTranslations` to echo the key — so neither key existed in
 * either locale and nothing noticed. `ComingSoon` reads `common.comingSoon.*`.
 *
 * Deliberately NOT under `gate.*`: "coming soon" is not one of the five
 * refusal reasons, and copy there would invite a sixth gate state.
 */
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import en from "./en.json";
import ja from "./ja.json";

type Messages = Record<string, unknown>;

describe.each([
  ["en", en],
  ["ja", ja],
] as const)("%s coming-soon labels", (locale, messages) => {
  it.each([
    ["sidebar", "comingSoon"],
    ["sidebar", "soon"],
    ["common", "comingSoon.title"],
    ["common", "comingSoon.description"],
  ] as const)("%s.%s resolves to real copy", (namespace, key) => {
    const t = createTranslator({
      locale,
      messages: messages as Messages,
      namespace,
      onError: (error) => {
        throw error;
      },
    });
    const text = t(key as never);
    expect(text).not.toBe(`${namespace}.${key}`);
    expect(text.trim()).not.toBe("");
  });

  it("keeps coming-soon copy out of gate.*", () => {
    const gateKeys = (function keys(node: unknown, path: string[]): string[] {
      if (typeof node !== "object" || node === null) return [path.join(".")];
      return Object.entries(node).flatMap(([k, v]) => keys(v, [...path, k]));
    })(messages.gate, ["gate"]);
    expect(gateKeys.filter((k) => /(comingSoon|soon)$/i.test(k))).toEqual([]);
  });
});
