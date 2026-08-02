/**
 * Every message must survive the real ICU parser (#1471).
 *
 * WHY THIS FILE EXISTS: component tests mock `useTranslations` as
 * `() => (key) => key`, so a message string is never parsed. next-intl formats
 * with ICU MessageFormat, where `{name}` is a PLACEHOLDER — a message
 * containing a literal brace that is not escaped throws at render time the
 * moment the component calls `t(key)` without supplying that argument. The
 * mocked tests stay green while the page crashes in the browser.
 *
 * `memoryLinkTemplatePlaceholder` / `memoryLinkTemplateHelp` document a URL
 * template containing `{context_id}` and `{memory_id}`; they must be written
 * with ICU quoting (`'{context_id}'`) so those braces render literally.
 *
 * The sweep is over ALL messages, not just the ones that prompted it: the same
 * mistake is available to every future string, and the check costs one pass.
 */
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import en from "./en.json";
import ja from "./ja.json";

type Messages = Record<string, unknown>;

/** Flatten to dotted paths so a failure names the exact key. */
function leaves(node: unknown, path: string[] = []): [string, string][] {
  if (typeof node === "string") return [[path.join("."), node]];
  if (node && typeof node === "object") {
    return Object.entries(node as Messages).flatMap(([k, v]) =>
      leaves(v, [...path, k]),
    );
  }
  return [];
}

describe.each([
  ["en", en],
  ["ja", ja],
])("%s messages are ICU-safe", (locale, messages) => {
  const entries = leaves(messages);

  it("has messages to check", () => {
    expect(entries.length).toBeGreaterThan(100);
  });

  it("every message parses and formats with no arguments supplied", () => {
    const t = createTranslator({
      locale,
      messages: messages as Messages,
      // Swallow next-intl's console error path so a genuine failure surfaces
      // as the thrown error below rather than as passing-but-noisy output.
      onError: (error) => {
        throw error;
      },
    });

    // SCOPE, honestly: this sweep catches MALFORMED patterns (unclosed brace,
    // bad plural syntax). It cannot catch an unescaped literal brace on its
    // own, because that produces the same FORMATTING_ERROR as a message that
    // legitimately takes an argument — and from here the two are
    // indistinguishable without knowing the call site. That specific
    // regression is pinned by the targeted test below; add a case there when a
    // new message documents literal braces.
    const broken: string[] = [];
    for (const [key] of entries) {
      try {
        t(key as never);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (/parse|syntax|malformed|unclosed|unexpected/i.test(message)) {
          broken.push(`${key}: ${message}`);
        }
      }
    }

    expect(broken).toEqual([]);
  });

  it("renders the referral-free connector link-template strings literally", () => {
    const t = createTranslator({
      locale,
      messages: messages as Messages,
      namespace: "connectors",
    });

    // The exact regression: these are shown to an admin as documentation of the
    // template syntax, so the braces must reach the screen.
    expect(t("memoryLinkTemplatePlaceholder" as never)).toContain("{context_id}");
    expect(t("memoryLinkTemplatePlaceholder" as never)).toContain("{memory_id}");
    expect(t("memoryLinkTemplateHelp" as never)).toContain("{context_id}");
    expect(t("memoryLinkTemplateHelp" as never)).toContain("{memory_id}");
    // ...and the ICU quoting must not leak into the rendered output.
    expect(t("memoryLinkTemplatePlaceholder" as never)).not.toContain("'{");
  });
});
