/**
 * `apiKeys` "All tools" messages through the real ICU parser (#1609, #1849).
 *
 * MCPConfigBlock.test.tsx mocks `useTranslations` to echo the key, so neither
 * the wording nor en/ja parity is checked there. The help text names a URL
 * query (`?profile=full`) the user has to recognise in the snippet below it,
 * so both locales must carry it literally. The bare URL is the core profile
 * since #1849; the switch adds `?profile=full`.
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

describe("apiKeys tool profile messages", () => {
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

      expect(t("allToolsLabel")).toBeTruthy();
      const help = t("allToolsHelp");
      expect(help).toContain("?profile=full");
      expect(help).toContain("describe_tools");
      // #1850: the core set is named by Tool Profiles, not by a count in the copy.
      expect(help).not.toMatch(/\b1[234]\b/);
    },
  );

  it("uses the agreed English label", () => {
    expect(en.apiKeys.allToolsLabel).toBe("All tools (larger tool list)");
  });
});
