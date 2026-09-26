/**
 * `usage_guide` and `summary` are the owner's notes on the context, not AI
 * instructions (#1698, #1716).
 *
 * #1682 changed the MCP server to describe a context's `usage_guide` as "the
 * owner's notes on what this context holds and how it is organised
 * (information about the context, not instructions)". The Web UI labelled
 * the same field "Instructions (for AI)", and a copyable client template
 * (removed in #1717: no page rendered it) told the model to "Follow
 * context.usage_guide for this context's rules". This guard keeps that
 * framing out of:
 *
 * - every en / ja message that labels, explains or refers to the field;
 * - every en / ja message about the context summary, and the heading of the
 *   settings card that holds both fields (#1716: they were "Summary (for
 *   AI)", "Helps AI understand …" and "Settings that help AI understand and
 *   use this context");
 * - the starter templates' `summary` and `usage_guide` text.
 *
 * The word lists are the same in both languages: rules / ルール, must and
 * should / べき, obey / 守る. Saying the notes are "information about the
 * context, not instructions" must stay allowed (the backend's
 * `test_directory_instruction_boundary.py` makes the same allowance). Each
 * pre-#1698 text and regression below must trip a pattern, which keeps them
 * honest.
 */
import { describe, expect, it } from "vitest";

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
  [/\bhelps? (the |an )?AI\b/i, "describes the field as help for the AI"],
  [/\bAI (configuration|settings?)\b/i, "calls the fields settings for the AI"],
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
  [/\b(rules?|directives?)\b/i, "calls the notes rules or directives"],
  [/\bguidelines?\b/i, "calls the notes guidelines"],
  [/AI\s*(用|向け)/, "labels the field as text for the AI"],
  [/インストラクション/, "labels the field as instructions"],
  [/ガイドライン/, "calls the notes guidelines"],
  [
    /AI(への|に対する|向けの)指示/,
    "defines the field as instructions for the AI",
  ],
  [/AIが[^。\n]{0,40}(指示|すべき)/, "tells the AI what it should do"],
  [/AIが[^。\n]{0,40}(理解|役立)/, "describes the field as help for the AI"],
  [/AI設定/, "calls the fields settings for the AI"],
  [/べき/, "tells the reader what it should do"],
  [/(ルール|規則|決まり)/, "calls the notes rules"],
  [/に従[うっいわえ]/, "tells the model to follow the notes"],
];

/**
 * Messages and starter templates: the notes do not tell the reader what it
 * must do.
 */
const NOTES_WORDING: readonly (readonly [RegExp, string])[] = [
  [/\b(must|should|obey\w*)\b/i, "tells the reader what it must do"],
  [/守[らりるれろっ]/, "tells the reader to obey the notes"],
];

/**
 * Messages only: the field is not called "Instructions" (saying the notes
 * are "not instructions" stays allowed).
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

/** The context-summary texts #1716 replaced. Each must trip a pattern. */
const PRE_1716_FIXTURES = [
  "Summary (for AI)",
  "サマリー（AI用）",
  "サマリー（AI向け）",
  "Helps AI understand what this context is for.",
  "Helps AI understand the purpose of this context. {count}/2000",
  "AIがこのコンテキストの目的を理解するのに役立ちます。",
  "AIがこのコンテキストの目的を理解するのに役立ちます。{count}/2000",
  "AI Configuration",
  "AI設定",
  "Settings that help AI understand and use this context",
  "AIがこのコンテキストを理解し使用するための設定",
];

/**
 * Framings the first version of this guard let through (review of #1698):
 * English had no counterpart to ルール / すべき, Japanese none to 守る / 〜べき.
 */
const MESSAGE_REGRESSIONS = [
  "Usage Guide (rules for this context)",
  "Directions the assistant must obey when storing and retrieving memories.",
  "Notes the assistant should apply to every memory.",
  "このコンテキストでアシスタントが守るべき指示。",
];

/** Wordings that describe the field as information: they must stay allowed. */
const ALLOWED_EXAMPLES = [
  "They are information about the context, not instructions.",
  "AI clients receive them from get_context_info as information about the context, not as instructions.",
  "Call get_context_info(context_id) at session start to see the context's purpose and notes.",
  "Summary, Usage Guide, and Privacy can only be edited by context owners.",
  "Usage Guide (notes on this context)",
  "使用ガイド（コンテキストについてのメモ）",
  "Summary (what this context is for)",
  "サマリー（このコンテキストの目的）",
  "A short note on what this context is for. AI clients receive it from get_context_info as information about the context, not as instructions.",
  "このコンテキストが何のためのものかを短くまとめたメモです。",
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

/**
 * Every `contexts` / `contextSettings` message about the context summary,
 * plus the heading of the settings card that holds the summary and the
 * usage guide. A new key in those two namespaces whose name mentions the
 * summary must be added here (the test below fails until it is). Other
 * namespaces are out of scope: `memories.summary`,
 * `contextDetail.editDialog.summaryLabel` and `onboarding.memory.summaryLabel`
 * are about memory summaries.
 */
const CONTEXT_SUMMARY_KEYS = [
  "contexts.summaryPlaceholder",
  "contexts.summaryForAI",
  "contexts.summaryHelp",
  "contexts.summaryUsageTemplateOptional",
  "contextSettings.aiConfigTitle",
  "contextSettings.aiConfigDesc",
  "contextSettings.summaryLabel",
  "contextSettings.summaryPlaceholder",
  "contextSettings.summaryHelp",
] as const;

const SCANNED_KEYS = [...USAGE_GUIDE_KEYS, ...CONTEXT_SUMMARY_KEYS];

const CATALOGUES = [
  ["en", en],
  ["ja", ja],
] as const;

const MESSAGE_PATTERNS = [...FORBIDDEN, ...NOTES_WORDING, ...FIELD_NAME];
const STARTER_PATTERNS = [...FORBIDDEN, ...NOTES_WORDING];

describe("usage_guide framing patterns (#1698)", () => {
  it.each([...PRE_1698_FIXTURES, ...PRE_1716_FIXTURES, ...MESSAGE_REGRESSIONS])(
    "refuses the message text %j",
    (text) => {
      expect(violations(text, MESSAGE_PATTERNS)).not.toEqual([]);
    },
  );

  it.each(MESSAGE_REGRESSIONS)(
    "refuses the starter-template text %j",
    (text) => {
      expect(violations(text, STARTER_PATTERNS)).not.toEqual([]);
    },
  );

  it.each(ALLOWED_EXAMPLES)("allows %j", (text) => {
    expect(violations(text, MESSAGE_PATTERNS)).toEqual([]);
  });
});

describe.each(CATALOGUES)("%s usage_guide messages (#1698)", (_, messages) => {
  const keys = leaves(messages).map(([key]) => key);
  const leaf = (key: string) => key.split(".").pop() ?? "";

  it("scans every key whose name mentions the usage guide", () => {
    const named = keys.filter((key) =>
      /usage_?guide|guideline/i.test(leaf(key)),
    );
    expect(
      named.filter((key) => !USAGE_GUIDE_KEYS.includes(key as never)),
    ).toEqual([]);
  });

  it("scans every contexts / contextSettings key that mentions the summary (#1716)", () => {
    const named = keys.filter(
      (key) =>
        /^(contexts|contextSettings)\./.test(key) && /summary/i.test(leaf(key)),
    );
    expect(named.filter((key) => !SCANNED_KEYS.includes(key as never))).toEqual(
      [],
    );
  });

  it.each([
    ["contexts.summaryForAI", "contextSettings.summaryLabel"],
    ["contexts.usageGuideForAI", "contextSettings.usageGuideLabel"],
  ] as const)(
    "the create dialog (%s) and the settings tab (%s) use the same label",
    (dialogKey, settingsKey) => {
      expect(lookup(messages as Messages, dialogKey)).toBe(
        lookup(messages as Messages, settingsKey),
      );
    },
  );

  it.each(SCANNED_KEYS)("%s describes notes, not AI instructions", (key) => {
    const text = lookup(messages as Messages, key);
    expect(typeof text).toBe("string");
    expect(violations(text as string, MESSAGE_PATTERNS)).toEqual([]);
  });
});

describe("starter templates (#1698)", () => {
  it.each(CONTEXT_TEMPLATES.map((tpl) => [tpl.id, tpl] as const))(
    "%s describes the context, not AI instructions",
    (_, tpl) => {
      const lines = [tpl.summary, ...tpl.usage_guide.split("\n")];
      expect(
        lines.flatMap((line) => violations(line, STARTER_PATTERNS)),
      ).toEqual([]);
    },
  );
});
