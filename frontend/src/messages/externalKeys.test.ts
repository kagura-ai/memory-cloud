/**
 * `externalKeys` messages: en/ja key parity (#1613).
 *
 * The page test mocks `useTranslations` to echo the key, so a key that exists
 * in one locale only would pass there and render as a raw key in the browser.
 * #1613 swapped `openAICannotDisable` for `protectedHint` (the row is locked by
 * the API's `is_protected`, not by being an OpenAI key); this pins that both
 * locales made the same swap. #1616 dropped the unused `featureDisabled` (the
 * page renders `provisioningDisabled` with BYOK off, never a whole-page block).
 */
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

describe("externalKeys messages", () => {
  it("has the same keys in en and ja", () => {
    expect(keys(ja.externalKeys).sort()).toEqual(keys(en.externalKeys).sort());
  });

  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)("%s carries the strings the protected row renders", (_, m) => {
    expect(m.externalKeys.required).toBeTruthy();
    expect(m.externalKeys.protectedHint).toBeTruthy();
    expect(m.externalKeys.provisioningDisabled).toBeTruthy();
    expect(m.externalKeys).not.toHaveProperty("openAICannotDisable");
    expect(m.externalKeys).not.toHaveProperty("featureDisabled");
  });
});
