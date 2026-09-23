/**
 * Message catalogue parity, orphans and the `gate.*` contract (#1646).
 *
 * WHY: component tests mock `useTranslations` as `() => (key) => key`, so a
 * key present in one locale only passes every test and renders as a raw
 * dotted key in the other language. #1613 pinned that for one namespace
 * (externalKeys.test.ts); this file makes it global, and adds the rules that
 * keep `gate.*` — the one namespace every refusal notice renders — honest:
 * five ICU arguments only, no tier names, a CTA only where one can exist,
 * every refusal state and every gate noun present, and every message
 * selection formattable with exactly the arguments its branch has.
 *
 * SCOPE, honestly: the orphan half is a REFERENCE scan, not a resolver. It
 * cannot follow a dynamic key (`t(`states.${x}.title`)`), so its answer is
 * "definitely referenced" or "not obviously referenced", and the second
 * bucket needs an allowlist entry with an owner. What fails the build is a
 * NEWLY orphaned key: one neither referenced nor allowlisted.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { createTranslator, type Messages as IntlMessages } from "next-intl";
import { describe, expect, it } from "vitest";

import {
  gateMessageKeys,
  type GateMessageKey,
} from "@/components/common/FeatureGateNotice";
import { WorkspaceRole } from "@/lib/auth/rbac";
import { GATE_KEYS, type RefusedGateState } from "@/lib/gates/featureGates";
import { DEFAULT_PLAN_LABELS } from "@/lib/utils/planLabel";

import en from "./en.json";
import ja from "./ja.json";

const CATALOGUES = [
  ["en", en],
  ["ja", ja],
] as const;

/** Every leaf, as [dotted key, message]. */
function leaves(node: unknown, path: string[] = []): [string, string][] {
  if (typeof node === "string") return [[path.join("."), node]];
  if (node && typeof node === "object") {
    return Object.entries(node as Record<string, unknown>).flatMap(([k, v]) =>
      leaves(v, [...path, k]),
    );
  }
  return [];
}

/** The message at a dotted path, or undefined. */
function messageAt(messages: unknown, key: string): unknown {
  let node: unknown = messages;
  for (const part of key.split(".")) {
    if (!node || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return node;
}

// ── The orphan scan (assertions 2 and 3) ────────────────────────────────────

/**
 * Keys no reference scan can see (reached only through a dynamic key), each
 * with the issue that owns it. Exact keys only — never a bare prefix.
 * #1646 stage 3 moves this list to `orphans.allowlist.ts`.
 */
const KNOWN_ORPHANS: ReadonlyArray<{
  readonly key: string;
  readonly owner: string;
}> = [];

const SRC = join(__dirname, "..");

/**
 * Every string literal in production source: `"…"`, `'…'`, and the static
 * pieces of template literals. Tests and this directory are skipped — a key
 * referenced only by a test is still dead in the product.
 */
function sourceLiterals(): Set<string> {
  const literals = new Set<string>();
  const skipDirs = new Set([join(SRC, "messages"), join(SRC, "test")]);
  (function walk(dir: string) {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) {
        if (!skipDirs.has(path)) walk(path);
        continue;
      }
      if (!/\.tsx?$/.test(entry) || /\.test\.tsx?$/.test(entry)) continue;
      const code = readFileSync(path, "utf8");
      for (const m of code.matchAll(
        /"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'/g,
      )) {
        literals.add(m[1] ?? m[2]);
      }
      for (const m of code.matchAll(/`((?:[^`\\]|\\.)*)`/g)) {
        for (const piece of m[1].split(/\$\{[^}]*\}/)) literals.add(piece);
      }
    }
  })(SRC);
  return literals;
}

/**
 * Referenced when some quoted literal is the full dotted key or any suffix
 * of it after a namespace boundary — `t("planGate.title")` under
 * `useTranslations("resources")` references `resources.planGate.title`, and
 * so does the bare leaf `t("title")`.
 */
function isReferenced(key: string, literals: ReadonlySet<string>): boolean {
  const parts = key.split(".");
  return parts.some((_, i) => literals.has(parts.slice(i).join(".")));
}

// ── Tier words (assertion 5) ────────────────────────────────────────────────

/**
 * Two patterns, because JS `\b` is defined against `[A-Za-z0-9_]` and never
 * fires beside kana / kanji: the ASCII one runs over both locales, the
 * boundary-free Japanese one over ja only.
 */
const TIER_ASCII = /\b(free|basic|pro|promax)\b/i;
const TIER_JA = /プロプラン|プロ|お試し|ベーシック|フリー/;

function tierWords(locale: string, message: string): boolean {
  return TIER_ASCII.test(message) || (locale === "ja" && TIER_JA.test(message));
}

/**
 * Every pre-#1646 key that carried gate copy, now superseded by `gate.*`:
 * the tier-naming ones and the duplicates of the unified copy, #1644's
 * interim keys, and the dead tier-naming keys.
 */
const SUPERSEDED_GATE_KEYS: readonly string[] = [
  "resources.planGate.title",
  "resources.planGate.description",
  "resources.planGate.action",
  "connectors.planGate.title",
  "connectors.planGate.description",
  "connectors.planGate.action",
  "resourceTokens.planGateTitle",
  "resourceTokens.planGateDesc",
  "resourceTokens.upgradePlan",
  "resourceTokens.createDialog.quotaLimitReached",
  "contextSettings.sharedRequiresPlan",
  "contextSettings.publicRequiresPlan",
  "contexts.proPlan",
  "contexts.pro",
  "contexts.upgradeToPro",
  "contexts.requiresProPlan",
  "contexts.upgradeToProCta",
  "contexts.teamMembersCanAccessShort",
  "contexts.quotaReachedDetail",
  "contexts.quotaReachedPlansLink",
  "contexts.contextLimitReached",
  "contexts.viewPlans",
  "contexts.quotaDialogTitle",
  "contexts.quotaDialogDescription",
  "contexts.quotaDialogUpgradeHeading",
  "contexts.quotaDialogUpgradeBody",
  "workspace.proPlanRequired",
  "workspace.ownerAdminOnly",
  "workspace.seatLimitReachedDesc",
  "workspace.upgradeToAddMembers",
  "workspace.sleepReports.planGate.title",
  "workspace.sleepReports.planGate.description",
  "workspace.sleepReports.planGate.action",
  "workspace.planPage.featureDisabled",
  "searchSettings.rerankerNotAvailableFree",
  "searchSettings.upgradeToBasic",
  "analyses.states.notEnabled.title",
  "analyses.states.notEnabled.description",
  "analyses.modal.footerHint",
  "admin.cost.featureDisabled",
  "admin.cost.costDisplayDisabled",
  "contextSettings.sleepQuotaTierBlocked",
  "resourceTokens.createDialog.quotaHelpFree",
  "resourceTokens.createDialog.quotaHelpBasic",
  "resourceTokens.createDialog.quotaHelpPro",
  "resourceTokens.proPlanRequired",
  "resourceTokens.proPlanRequiredDesc",
  "workspace.proPlanRequiredDesc",
  "analyses.states.ownerOnly.title",
  "analyses.states.ownerOnly.description",
  "analyses.states.planRequired.title",
  "analyses.states.planRequired.description",
  "workspace.invitePlanRequired",
  "workspace.memberSeatsFull",
  "connectors.connectorPlanRequired",
  "connectors.connectorSeatsFull",
];

// ── The gate.* contract ─────────────────────────────────────────────────────

const ICU_ARGS = ["feature", "plan", "currentPlan", "current", "limit"];

/**
 * The argument names an ICU message interpolates. A regex over `{name` would
 * read the option bodies of `{current, plural, one {is} other {are}}` as
 * arguments `is` and `are`, so this walks the message: an argument's name
 * is recorded, and the bodies of a `plural` / `select` / `selectordinal`
 * are walked as messages in turn. (No gate.* message uses ICU quoting.)
 */
function icuArgs(message: string): string[] {
  const names: string[] = [];
  // Walks a message from `i` to its closing `}` (or the end); returns the
  // index after it.
  const walk = (i: number): number => {
    while (i < message.length) {
      const c = message[i];
      if (c === "}") return i + 1;
      if (c !== "{") {
        i += 1;
        continue;
      }
      const head = /^\{\s*([^\s,{}]+)\s*(?:,\s*(\w+)\s*)?/.exec(
        message.slice(i),
      );
      if (!head) throw new Error(`unparseable argument in: ${message}`);
      names.push(head[1]);
      i += head[0].length;
      if (head[2] === undefined || message[i] === "}") {
        i += 1;
        continue;
      }
      const branching = ["plural", "select", "selectordinal"].includes(head[2]);
      i += 1; // the "," after the type
      while (i < message.length && message[i] !== "}") {
        if (message[i] === "{") {
          if (branching) i = walk(i + 1);
          else throw new Error(`nested style in: ${message}`);
        } else {
          i += 1;
        }
      }
      i += 1;
    }
    return i;
  };
  walk(0);
  return names;
}

/** The refusal subtrees (`role` has no direct leaves). */
const REFUSAL_SUBTREES = [
  "plan",
  "quota",
  "deployment",
  "role.owner",
  "role.admin",
  "allowlist",
] as const;

function gateTranslator(locale: string, messages: unknown) {
  return createTranslator({
    locale,
    messages: messages as IntlMessages,
    namespace: "gate",
    onError: (error) => {
      throw error;
    },
  });
}

describe("message catalogues", () => {
  // 1
  it("en and ja have identical key sets", () => {
    const enKeys = leaves(en)
      .map(([key]) => key)
      .sort();
    const jaKeys = leaves(ja)
      .map(([key]) => key)
      .sort();
    expect(jaKeys.filter((key) => !enKeys.includes(key))).toEqual([]);
    expect(enKeys.filter((key) => !jaKeys.includes(key))).toEqual([]);
    expect(jaKeys).toEqual(enKeys);
  });

  // 2 — enabled by #1646 stage 3
  it.skip("every message key is referenced from src/, or allowlisted", () => {
    const literals = sourceLiterals();
    const allowlisted = new Set(KNOWN_ORPHANS.map((entry) => entry.key));
    const orphans = leaves(en)
      .map(([key]) => key)
      .filter((key) => !isReferenced(key, literals) && !allowlisted.has(key));
    // Delete the key, or allowlist it in orphans.allowlist.ts with the issue
    // that owns it.
    expect(orphans).toEqual([]);
  });

  // 3 — enabled by #1646 stage 3
  it.skip("the orphan allowlist has no stale entries", () => {
    const literals = sourceLiterals();
    const keys = new Set(leaves(en).map(([key]) => key));
    for (const { key, owner } of KNOWN_ORPHANS) {
      expect(owner, key).toMatch(/^#\d+$/);
      expect(keys.has(key), `${key} no longer exists`).toBe(true);
      expect(isReferenced(key, literals), `${key} is referenced now`).toBe(
        false,
      );
    }
  });

  // 5, outside gate.* — enabled by #1646 stage 3
  it.skip("no superseded gate key survives outside gate.* (each named a tier or duplicated gate copy)", () => {
    for (const [locale, messages] of CATALOGUES) {
      const surviving = SUPERSEDED_GATE_KEYS.filter(
        (key) => messageAt(messages, key) !== undefined,
      );
      expect(surviving, locale).toEqual([]);
    }
  });

  it("usageStats.sleepContextsTier, a caption shown on every tier, names no tier", () => {
    for (const [locale, messages] of CATALOGUES) {
      const caption = messages.usageStats.sleepContextsTier;
      expect(tierWords(locale, caption), `${locale}: ${caption}`).toBe(false);
    }
  });
});

describe.each(CATALOGUES)("%s gate.* contract", (locale, messages) => {
  const { features: _nouns, ...notices } = messages.gate;
  const gateLeaves = leaves(messages.gate, ["gate"]);

  // 4
  it("interpolates only {feature}, {plan}, {currentPlan}, {current} and {limit}", () => {
    const used = new Set(gateLeaves.flatMap(([, message]) => icuArgs(message)));
    expect([...used].filter((name) => !ICU_ARGS.includes(name))).toEqual([]);

    // And by the real ICU formatter: with the five supplied, nothing is
    // missing — so no message can need a sixth.
    const t = gateTranslator(locale, messages);
    for (const [key] of leaves(notices)) {
      expect(() =>
        t(key, {
          feature: "F",
          plan: "P",
          currentPlan: "C",
          current: 1,
          limit: 2,
        }),
      ).not.toThrow();
    }
  });

  it("reads ICU plural option bodies as text, not as arguments", () => {
    expect(
      icuArgs("{current} {current, plural, one {is} other {are}} of {limit}"),
    ).toEqual(["current", "current", "limit"]);
  });

  // 5
  it("names no plan tier", () => {
    const named = gateLeaves.filter(([, message]) =>
      tierWords(locale, message),
    );
    expect(named).toEqual([]);
  });

  // 5, widened: the spec's two regexes cover the registry keys and the ja
  // words, not the LABELS a default deployment renders (planLabel.ts: S / M /
  // L / XL) or the other documented label sets (Trial / Starter, 無料 /
  // スターター). A hardcoded "XL" pins a tier as surely as "Pro" does.
  it("names no plan tier label either (S / M / L / XL, Trial, Starter, 無料 …)", () => {
    const labels = Object.values(DEFAULT_PLAN_LABELS).join("|");
    const asciiLabel = new RegExp(
      `(?<![A-Za-z0-9_])(${labels})(?![A-Za-z0-9_])`,
    );
    const named = gateLeaves.filter(([, message]) => {
      const prose = message.replace(/\{[^}]*\}/g, "");
      return (
        asciiLabel.test(prose) ||
        /\b(trial|starter)\b/i.test(prose) ||
        (locale === "ja" && /無料|スターター/.test(prose))
      );
    });
    expect(named).toEqual([]);
  });

  // 6
  it("has an action only under gate.plan and gate.quota", () => {
    const actions = gateLeaves
      .map(([key]) => key)
      .filter((key) => key.endsWith(".action"))
      .sort();
    expect(actions).toEqual(["gate.plan.action", "gate.quota.action"]);
  });

  // 7
  it.each(REFUSAL_SUBTREES)(
    "gate.%s has a title, a description, a badge and a hint",
    (subtree) => {
      for (const slot of ["title", "description", "badge", "hint"]) {
        const message = messageAt(messages.gate, `${subtree}.${slot}`);
        expect(typeof message, `gate.${subtree}.${slot}`).toBe("string");
        expect((message as string).trim()).not.toBe("");
      }
    },
  );

  // 8
  it("has a label, a plural and a singular noun for every GateKey, and no other", () => {
    expect(Object.keys(messages.gate.features).sort()).toEqual(
      [...GATE_KEYS].sort(),
    );
    for (const key of GATE_KEYS) {
      expect(Object.keys(messages.gate.features[key]).sort(), key).toEqual([
        "label",
        "plural",
        "singular",
      ]);
      for (const form of ["label", "plural", "singular"]) {
        const noun = messageAt(messages.gate.features, `${key}.${form}`);
        expect(typeof noun, `gate.features.${key}.${form}`).toBe("string");
        expect((noun as string).trim()).not.toBe("");
      }
    }
  });

  // 9
  it("formats every key set gateMessageKeys can return with exactly the arguments its branch has", () => {
    const t = gateTranslator(locale, messages);
    const states: readonly RefusedGateState[] = [
      "plan",
      "quota",
      "deployment",
      "role",
      "allowlist",
    ];
    const roles = [
      WorkspaceRole.Owner,
      WorkspaceRole.Admin,
      undefined,
    ] as const;
    let formatted = 0;

    for (const state of states) {
      for (const scope of ["create", "all"] as const) {
        for (const role of roles) {
          for (const hasRequiredPlan of [true, false]) {
            for (const hasCurrentPlan of [true, false]) {
              for (const hasNumbers of [true, false]) {
                const keys = gateMessageKeys(
                  state,
                  scope,
                  role,
                  hasRequiredPlan,
                  hasCurrentPlan,
                  hasNumbers,
                );
                const values: Record<string, string | number> = {
                  feature: "F",
                  ...(hasRequiredPlan && { plan: "P" }),
                  ...(hasCurrentPlan && { currentPlan: "C" }),
                  ...(hasNumbers && { current: 1, limit: 2 }),
                };
                const selected = Object.values(keys).filter(
                  (key): key is GateMessageKey => key !== null,
                );
                for (const key of selected) {
                  const branch = `${state}/${scope}/${role}/plan=${hasRequiredPlan}/current=${hasCurrentPlan}/numbers=${hasNumbers}`;
                  expect(
                    () => t(key, values),
                    `${branch} → ${key}`,
                  ).not.toThrow();
                  formatted += 1;
                }
              }
            }
          }
        }
      }
    }
    expect(formatted).toBeGreaterThan(0);
  });
});
