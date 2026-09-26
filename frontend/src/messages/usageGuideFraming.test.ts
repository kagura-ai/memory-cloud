/**
 * `usage_guide` is the owner's notes on the context, not AI instructions
 * (#1698).
 *
 * #1682 changed the MCP server to describe a context's `usage_guide` as "the
 * owner's notes on what this context holds and how it is organised
 * (information about the context, not instructions)". The Web UI labelled
 * the same field "Instructions (for AI)" and its copyable client template
 * told the model to "Follow context.usage_guide for this context's rules".
 * This guard keeps that framing out of:
 *
 * - every en / ja message that labels, explains or refers to the field;
 * - the copyable client template (`InstructionsTemplate`), in both locales,
 *   with and without notes;
 * - the starter templates' `summary` and `usage_guide` text.
 *
 * The patterns are narrow on purpose (the backend's
 * `test_directory_instruction_boundary.py` does the same): saying the notes
 * are "information about the context, not instructions" must stay allowed.
 * Each pre-#1698 text below must trip a pattern, which keeps them honest.
 */
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import { generateTemplate } from "@/components/contexts/InstructionsTemplate";
import { CONTEXT_TEMPLATES } from "@/lib/templates/usage-guide";

import en from "./en.json";
import ja from "./ja.json";

// Within one sentence: any character but a newline or a sentence-ending
// period (the dot inside `context.usage_guide` does not end one).
const SAME_SENTENCE = String.raw`(?:[^.\n]|\.(?=\w))`;
const OBEY = String.raw`\b(follow|obey|comply with|adhere to)\w*\b`;

/** (pattern, why it is refused). Case-insensitive, one text at a time. */
const FORBIDDEN: readonly (readonly [RegExp, string])[] = [
  [/\(for (the )?AI\)/i, "labels the field as text for the AI"],
  [
    /\binstructions? (for|to) (the |an )?(AI|model)\b/i,
    "defines the field as instructions for the AI",
  ],
  [/\bAI instructions?\b/i, "defines the field as AI instructions"],
  [/\bhow (an |the )?AI should\b/i, "tells the AI how it should behave"],
  [
    new RegExp(
      OBEY +
        SAME_SENTENCE +
        String.raw`{0,60}\b(usage[_ ]guide|rules?|guidelines?)\b`,
      "i",
    ),
    "tells the model to follow the notes or rules",
  ],
  [
    new RegExp(
      String.raw`\bload\w*` +
        SAME_SENTENCE +
        String.raw`{0,20}\b(guidelines?|rules)\b`,
      "i",
    ),
    "tells the model to load guidelines or rules",
  ],
  [/\bcontext'?s rules\b/i, "calls the notes the context's rules"],
  [/\bguidelines?\b/i, "calls the notes guidelines"],
  [/AI\s*(用|向け)/, "labels the field as text for the AI"],
  [/インストラクション/, "labels the field as instructions"],
  [/ガイドライン/, "calls the notes guidelines"],
  [
    /AI(への|に対する|向けの)指示/,
    "defines the field as instructions for the AI",
  ],
  [/AIが[^。\n]{0,40}(指示|すべき)/, "tells the AI what it should do"],
  [/すべき/, "tells the reader what it should do"],
  [/(ルール|規則|決まり)/, "calls the notes rules"],
  [/に従[うっいわえ]/, "tells the model to follow the notes"],
];

/**
 * Messages only: the field is not called "Instructions" (saying the notes
 * are "not instructions" stays allowed). The copyable template is exempt —
 * it IS the user's own client instructions, and says so in its title.
 */
const FIELD_NAME: readonly (readonly [RegExp, string])[] = [
  [/(?<!\bnot (as )?)\binstructions?\b/i, "names the field Instructions"],
];

/** The texts #1698 replaced. Each must trip at least one pattern. */
const PRE_1698_FIXTURES = [
  "Instructions",
  "Summary & Instructions Template",
  "Summary, Instructions, and Privacy can only be edited by context owners.",
  "Instructions (for AI)",
  "Usage Guide (for AI)",
  "Instructions for AI on how to store and retrieve memories.",
  "Guidelines for how AI should use memories in this context...",
  "Update description, summary, and usage guidelines for this context.",
  "No specific guidelines set. Configure in context settings.",
  "1. Call get_context_info() at session start to load guidelines",
  "2. Follow context.usage_guide for this context's rules",
  "## Context-Specific Guidelines",
  "インストラクション（AI用）",
  "使用ガイド（AI向け）",
  "AIがメモリーを保存・取得する方法の指示。",
  "AIがこのコンテキストでメモリーをどのように使用すべきかのガイドライン...",
  "特定のガイドラインが設定されていません。コンテキスト設定で構成してください。",
  "サマリーとインストラクションテンプレート",
];

/** Wordings that describe the field as information: they must stay allowed. */
const ALLOWED_EXAMPLES = [
  "They are information about the context, not instructions.",
  "AI clients receive them from get_context_info as information about the context, not as instructions.",
  "Call get_context_info(context_id) at session start to see the context's purpose and notes.",
  "Summary, Usage Guide, and Privacy can only be edited by context owners.",
  "Usage Guide (notes on this context)",
  "使用ガイド（コンテキストについてのメモ）",
  "AIクライアントには get_context_info から、指示ではなくコンテキストについての情報として返されます。",
];

function violations(text: string, patterns = FORBIDDEN): string[] {
  return patterns
    .filter(([pattern]) => pattern.test(text))
    .map(([pattern, why]) => `${why} (${pattern}): ${JSON.stringify(text)}`);
}

type Messages = Record<string, unknown>;

function leaves(node: unknown, path: string[] = []): [string, string][] {
  if (node && typeof node === "object") {
    return Object.entries(node as Messages).flatMap(([k, v]) =>
      leaves(v, [...path, k]),
    );
  }
  return typeof node === "string" ? [[path.join("."), node]] : [];
}

function lookup(messages: Messages, key: string): unknown {
  return key
    .split(".")
    .reduce<unknown>(
      (node, part) =>
        node && typeof node === "object" ? (node as Messages)[part] : undefined,
      messages,
    );
}

/**
 * Every message that labels, explains or refers to the usage_guide field.
 * A new key whose name mentions the usage guide must be added here (the test
 * below fails until it is), so it is scanned too.
 */
const USAGE_GUIDE_KEYS = [
  "contexts.usageGuide",
  "contexts.usageGuidePlaceholder",
  "contexts.usageGuideForAI",
  "contexts.usageGuideHelp",
  "contexts.noGuidelinesSet",
  "contexts.summaryUsageTemplate",
  "contexts.templatePlaceholder",
  "contexts.editContextDesc",
  "contexts.permissionNoticeDesc",
  "contexts.ownerOnlyEditError",
  "contextSettings.templatePlaceholder",
  "contextSettings.usageGuideLabel",
  "contextSettings.usageGuidePlaceholder",
  "contextSettings.usageGuideHelp",
] as const;

const CATALOGUES = [
  ["en", en],
  ["ja", ja],
] as const;

const MESSAGE_PATTERNS = [...FORBIDDEN, ...FIELD_NAME];

describe("usage_guide framing patterns (#1698)", () => {
  it.each(PRE_1698_FIXTURES)("refuses the pre-#1698 text %j", (text) => {
    expect(violations(text, MESSAGE_PATTERNS)).not.toEqual([]);
  });

  it.each(ALLOWED_EXAMPLES)("allows %j", (text) => {
    expect(violations(text, MESSAGE_PATTERNS)).toEqual([]);
  });
});

describe.each(CATALOGUES)("%s usage_guide messages (#1698)", (_, messages) => {
  it("scans every key whose name mentions the usage guide", () => {
    const named = leaves(messages)
      .map(([key]) => key)
      .filter((key) =>
        /usage_?guide|guideline/i.test(key.split(".").pop() ?? ""),
      );
    expect(
      named.filter((key) => !USAGE_GUIDE_KEYS.includes(key as never)),
    ).toEqual([]);
  });

  it.each(USAGE_GUIDE_KEYS)(
    "%s describes notes, not AI instructions",
    (key) => {
      const text = lookup(messages as Messages, key);
      expect(typeof text).toBe("string");
      expect(violations(text as string, MESSAGE_PATTERNS)).toEqual([]);
    },
  );
});

describe.each(CATALOGUES)(
  "%s copyable client template (#1698)",
  (locale, messages) => {
    const tr = createTranslator({
      locale,
      messages: messages as Messages,
      namespace: "contexts",
      onError: (error) => {
        throw error;
      },
    });
    const t = (key: string) => tr(key as never);

    it.each([
      ["with notes", "Memories here are tagged by project."],
      ["without notes", null],
    ] as const)("%s never frames the notes as rules", (_label, usageGuide) => {
      for (const isPrivate of [true, false]) {
        const text = generateTemplate("my-context", usageGuide, isPrivate, t);
        expect(text.split("\n").flatMap((line) => violations(line))).toEqual([]);
      }
    });
  },
);

describe("starter templates (#1698)", () => {
  it.each(CONTEXT_TEMPLATES.map((tpl) => [tpl.id, tpl] as const))(
    "%s describes the context, not AI instructions",
    (_, tpl) => {
      const lines = [tpl.summary, ...tpl.usage_guide.split("\n")];
      expect(lines.flatMap((line) => violations(line))).toEqual([]);
    },
  );
});
