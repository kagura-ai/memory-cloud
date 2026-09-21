/**
 * `apiKeys` "Core tools only" messages through the real ICU parser (#1609).
 *
 * MCPConfigBlock.test.tsx mocks `useTranslations` to echo the key, so neither
 * the wording nor en/ja parity is checked there. The help text names a URL
 * query (`?profile=core`) the user has to recognise in the snippet below it,
 * so both locales must carry it literally.
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

describe("apiKeys core tool profile messages", () => {
  it("has the same keys in en and ja", () => {
    expect(keys(ja.apiKeys).sort()).toEqual(keys(en.apiKeys).sort());
  });

  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)(
    "%s formats the switch label and help text",
    (locale, messages) => {
      const t = createTranslator({
        locale,
        messages,
        namespace: "apiKeys",
        onError: (error) => {
          throw error;
        },
      });

      expect(t("coreProfileLabel")).toBeTruthy();
      const help = t("coreProfileHelp");
      expect(help).toContain("?profile=core");
      expect(help).toContain("65%");
      expect(help).toContain("12");
    },
  );

  it("uses the agreed English label", () => {
    expect(en.apiKeys.coreProfileLabel).toBe(
      "Core tools only (smaller tool list)",
    );
  });
});
