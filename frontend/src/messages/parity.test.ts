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
 * is namespace-aware — a key counts only when a translator bound to its
 * namespace is called with it — but it cannot follow a runtime key
 * (`t(`states.${x}.title`)`) beyond its static prefix, so its answer is
 * "referenced" or "not referenced by any call it can read", and the second
 * bucket is the baseline in orphans.allowlist.ts. What fails the build is a
 * NEWLY orphaned key: one neither referenced nor allowlisted. The baseline
 * is a ratchet — entries may only be removed, and a stale one fails too.
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
import {
  GATE_KEYS,
  type GateKey,
  type RefusedGateState,
} from "@/lib/gates/featureGates";
import { DEFAULT_PLAN_LABELS } from "@/lib/utils/planLabel";

import en from "./en.json";
import ja from "./ja.json";
import { ORPHAN_ALLOWLIST } from "./orphans.allowlist";

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

const SRC = join(__dirname, "..");

/**
 * A token of TS / TSX source. Comments are dropped; a string keeps its raw
 * body; a template keeps its first static piece (`head`) and whether any
 * `${…}` follows it — the expressions themselves are lexed as streams of
 * their own, so a call inside one is still seen.
 */
interface Tok {
  readonly k: "id" | "str" | "tpl" | "p";
  readonly v: string;
  readonly pos: number;
  readonly dynamic?: boolean;
}

const REGEX_AFTER_WORD = new Set([
  "return",
  "typeof",
  "case",
  "in",
  "of",
  "new",
  "delete",
  "void",
  "throw",
  "else",
  "yield",
  "await",
]);

/** Can a `/` here start a regex literal (rather than divide)? */
function regexCanStart(prev: Tok | undefined): boolean {
  if (!prev) return true;
  // `</div>`: a JSX closing tag, not a regex.
  if (prev.k === "p") return !")]}<".includes(prev.v);
  return prev.k === "id" && REGEX_AFTER_WORD.has(prev.v);
}

/**
 * Lex `src` from `i`. With `inExpr`, stop at the `}` that closes a template
 * `${…}` and return the index after it. Template expressions are pushed onto
 * `streams` as separate token lists.
 */
function lex(
  src: string,
  i: number,
  streams: Tok[][],
  inExpr = false,
): { toks: Tok[]; end: number } {
  const toks: Tok[] = [];
  let depth = 0;
  while (i < src.length) {
    const c = src[i];
    if (/\s/.test(c)) {
      i += 1;
    } else if (c === "/" && src[i + 1] === "/") {
      while (i < src.length && src[i] !== "\n") i += 1;
    } else if (c === "/" && src[i + 1] === "*") {
      const close = src.indexOf("*/", i + 2);
      i = close === -1 ? src.length : close + 2;
    } else if (c === "/" && regexCanStart(toks[toks.length - 1])) {
      let inClass = false;
      i += 1;
      while (i < src.length && src[i] !== "\n") {
        if (src[i] === "\\") i += 2;
        else if (src[i] === "[") ((inClass = true), (i += 1));
        else if (src[i] === "]") ((inClass = false), (i += 1));
        else if (src[i] === "/" && !inClass) break;
        else i += 1;
      }
      i += 1;
      while (/[a-z]/i.test(src[i] ?? "")) i += 1;
    } else if (c === '"' || c === "'") {
      const start = i;
      i += 1;
      while (i < src.length && src[i] !== c && src[i] !== "\n") {
        i += src[i] === "\\" ? 2 : 1;
      }
      toks.push({ k: "str", v: src.slice(start + 1, i), pos: start });
      i += 1;
    } else if (c === "`") {
      const start = i;
      let head: string | null = null;
      let piece = "";
      let dynamic = false;
      i += 1;
      while (i < src.length && src[i] !== "`") {
        if (src[i] === "\\") {
          piece += src.slice(i, i + 2);
          i += 2;
        } else if (src[i] === "$" && src[i + 1] === "{") {
          head ??= piece;
          dynamic = true;
          const inner = lex(src, i + 2, streams, true);
          streams.push(inner.toks);
          i = inner.end;
        } else {
          piece += src[i];
          i += 1;
        }
      }
      i += 1;
      toks.push({ k: "tpl", v: head ?? piece, pos: start, dynamic });
    } else if (/[A-Za-z_$]/.test(c)) {
      const start = i;
      while (/[\w$]/.test(src[i] ?? "")) i += 1;
      toks.push({ k: "id", v: src.slice(start, i), pos: start });
    } else if (/[0-9]/.test(c)) {
      while (/[\w.]/.test(src[i] ?? "")) i += 1;
    } else {
      if (c === "{") depth += 1;
      if (c === "}") {
        if (inExpr && depth === 0) return { toks, end: i + 1 };
        depth -= 1;
      }
      toks.push({ k: "p", v: c, pos: i });
      i += 1;
    }
  }
  return { toks, end: i };
}

/** Every token stream of a file: the top level and each template expression. */
function tokenStreams(src: string): Tok[][] {
  const streams: Tok[][] = [];
  streams.unshift(lex(src, 0, streams).toks);
  return streams;
}

/** The tokens of the call argument starting at `i`, and the index after it. */
function readArg(toks: readonly Tok[], i: number): { arg: Tok[]; end: number } {
  const arg: Tok[] = [];
  let depth = 0;
  for (; i < toks.length; i += 1) {
    const t = toks[i];
    if (t.k === "p") {
      if (depth === 0 && (t.v === "," || t.v === ")")) break;
      if ("([{".includes(t.v)) depth += 1;
      if (")]}".includes(t.v)) depth -= 1;
    }
    arg.push(t);
  }
  return { arg, end: i };
}

/** Split `toks` at top-level punctuation `sep`. */
function splitTop(toks: readonly Tok[], sep: string): Tok[][] {
  const parts: Tok[][] = [[]];
  let depth = 0;
  for (const t of toks) {
    if (t.k === "p" && "([{".includes(t.v)) depth += 1;
    if (t.k === "p" && ")]}".includes(t.v)) depth -= 1;
    if (depth === 0 && t.k === "p" && t.v === sep) parts.push([]);
    else parts[parts.length - 1].push(t);
  }
  return parts;
}

/** A key argument read statically. */
type KeyRef = { readonly exact: string } | { readonly prefix: string };

/**
 * What a key argument references: a literal is one exact key; a template is
 * its static head as a prefix (`plan.${x}` → `plan.*`), or an exact key if
 * it has no `${…}`; each branch of a `?:` on its own; anything else is a
 * runtime key, i.e. the whole namespace (prefix "").
 */
function keyRefs(arg: readonly Tok[]): KeyRef[] {
  // `t(x as any)`: the cast is not part of the key.
  const as = arg.findIndex((t) => t.k === "id" && t.v === "as");
  const toks = as === -1 ? arg : arg.slice(0, as);
  if (toks.length === 1 && toks[0].k === "str") return [{ exact: toks[0].v }];
  if (toks.length === 1 && toks[0].k === "tpl") {
    return [toks[0].dynamic ? { prefix: toks[0].v } : { exact: toks[0].v }];
  }
  const [, ...afterQuestion] = splitTop(toks, "?");
  if (afterQuestion.length === 1) {
    const branches = splitTop(afterQuestion[0], ":");
    if (branches.length === 2) return branches.flatMap(keyRefs);
  }
  return [{ prefix: "" }];
}

/**
 * The namespaces a translator factory call binds. `useTranslations("a.b")`
 * / `getTranslations("a.b")` → `a.b`; `createTranslator({ namespace: "a" })`
 * or `getTranslations({ locale, namespace: "a" })` → `a`; no argument or no
 * `namespace` → the root (""). An identifier is resolved to the string
 * literals it is assigned in the same file (a `const`, or a destructured
 * prop's default — `translationNamespace = "admin.sleepReports"`); an
 * unresolvable one binds nothing.
 */
function boundNamespaces(
  arg: readonly Tok[],
  assigned: (name: string) => string[],
): string[] {
  if (arg.length === 0) return [""];
  const resolve = (t: Tok | undefined): string[] =>
    t?.k === "str"
      ? [t.v]
      : t?.k === "tpl" && !t.dynamic
        ? [t.v]
        : t?.k === "id"
          ? assigned(t.v)
          : [];
  if (arg.length === 1) return resolve(arg[0]);
  if (arg[0].k === "p" && arg[0].v === "{") {
    const at = arg.findIndex(
      (t, i) => t.k === "id" && t.v === "namespace" && i > 0,
    );
    if (at === -1) return [""];
    // `namespace: "x"` or the shorthand `namespace`.
    return arg[at + 1]?.v === ":"
      ? resolve(arg[at + 2])
      : assigned("namespace");
  }
  return [];
}

const FACTORIES = new Set([
  "useTranslations",
  "getTranslations",
  "createTranslator",
]);
const METHODS = new Set(["rich", "raw", "markup", "has"]);

/** Exact keys and key prefixes referenced from source, as full dotted keys. */
interface References {
  readonly exact: ReadonlySet<string>;
  readonly prefixes: ReadonlySet<string>;
}

/**
 * The namespace-aware reference scan. Per file:
 *
 * 1. Bindings — `<name> = [await] useTranslations(…) | getTranslations(…) |
 *    createTranslator(…)` — map a translator variable to its namespace(s).
 * 2. Calls — `<name>(key)` and `<name>.rich|raw|markup|has(key)` on a bound
 *    name — reference `<ns>.<key>`. A call takes the nearest binding of its
 *    name BEFORE it (two components in one file may bind `t` differently);
 *    a call above every binding of its name (a helper taking the component's
 *    `t`) takes all of them.
 *
 * A call on a name no factory binds in the file (a translator received as a
 * prop from another module) references nothing: the scan cannot know its
 * namespace, so a key reached only that way is an orphan to it. Comments are
 * never scanned, nor are string literals outside a translator call.
 */
function scanReferences(
  files: readonly { readonly code: string }[],
): References {
  const exact = new Set<string>();
  const prefixes = new Set<string>();

  for (const { code } of files) {
    const streams = tokenStreams(code);

    const assignments = new Map<string, string[]>();
    for (const toks of streams) {
      toks.forEach((t, i) => {
        const value = toks[i + 2];
        if (
          t.k === "id" &&
          toks[i + 1]?.v === "=" &&
          toks[i + 2]?.v !== "=" &&
          (value?.k === "str" || (value?.k === "tpl" && !value.dynamic)) &&
          // the literal is the whole right-hand side
          (toks[i + 3] === undefined ||
            [",", ";", ")", "}"].includes(toks[i + 3].v) ||
            toks[i + 3].k === "id")
        ) {
          assignments.set(t.v, [...(assignments.get(t.v) ?? []), value.v]);
        }
      });
    }
    const assigned = (name: string) => assignments.get(name) ?? [];

    const bindings: { name: string; pos: number; ns: string[] }[] = [];
    for (const toks of streams) {
      toks.forEach((t, i) => {
        if (t.k !== "id" || toks[i + 1]?.v !== "=") return;
        if (toks[i - 1]?.v === "." || toks[i + 2]?.v === "=") return;
        let j = i + 2;
        if (toks[j]?.v === "await") j += 1;
        if (!FACTORIES.has(toks[j]?.v ?? "") || toks[j + 1]?.v !== "(") return;
        const { arg } = readArg(toks, j + 2);
        bindings.push({
          name: t.v,
          pos: t.pos,
          ns: boundNamespaces(arg, assigned),
        });
      });
    }
    if (bindings.length === 0) continue;
    let rootRuntimeKey = false;

    const namespacesAt = (name: string, pos: number): string[] => {
      const own = bindings.filter((b) => b.name === name);
      const before = own.filter((b) => b.pos < pos);
      if (before.length === 0) return own.flatMap((b) => b.ns);
      return before.reduce((a, b) => (b.pos > a.pos ? b : a)).ns;
    };

    for (const toks of streams) {
      toks.forEach((t, i) => {
        if (t.k !== "id" || toks[i - 1]?.v === ".") return;
        let open = i + 1;
        if (
          toks[i + 1]?.v === "." &&
          METHODS.has(toks[i + 2]?.v ?? "") &&
          toks[i + 3]?.v === "("
        ) {
          open = i + 3;
        }
        if (toks[open]?.v !== "(" || toks[open].k !== "p") return;
        const namespaces = namespacesAt(t.v, t.pos);
        if (namespaces.length === 0) return;
        const refs = keyRefs(readArg(toks, open + 1).arg);
        for (const ns of namespaces) {
          const base = ns ? `${ns}.` : "";
          for (const ref of refs) {
            if ("exact" in ref) exact.add(base + ref.exact);
            else if (base + ref.prefix) prefixes.add(base + ref.prefix);
            else rootRuntimeKey = true;
          }
        }
      });
    }
    // A runtime key on the ROOT translator has no static prefix at all, and
    // as a wildcard it would reference every key there is. Such a key is
    // looked up from a map the file spells out (`SCOPE_LABEL_MAP[scope]`),
    // so the file's own string literals stand in for it instead.
    if (rootRuntimeKey) {
      for (const toks of streams) {
        for (const t of toks) {
          if (t.k === "str" || (t.k === "tpl" && !t.dynamic)) exact.add(t.v);
        }
      }
    }
  }
  return { exact, prefixes };
}

/** Is `key` referenced exactly, or under a runtime-key prefix? */
function isReferenced(key: string, refs: References): boolean {
  if (refs.exact.has(key)) return true;
  for (const prefix of refs.prefixes) if (key.startsWith(prefix)) return true;
  return false;
}

/**
 * Production source: every .ts / .tsx under src/ but tests, this directory
 * and src/test — a key referenced only by a test is still dead in the
 * product.
 */
function productionSources(): { path: string; code: string }[] {
  const files: { path: string; code: string }[] = [];
  const skipDirs = new Set([join(SRC, "messages"), join(SRC, "test")]);
  (function walk(dir: string) {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) {
        if (!skipDirs.has(path)) walk(path);
        continue;
      }
      if (!/\.tsx?$/.test(entry) || /\.test\.tsx?$/.test(entry)) continue;
      files.push({ path, code: readFileSync(path, "utf8") });
    }
  })(SRC);
  return files;
}

let cachedReferences: References | undefined;
function sourceReferences(): References {
  cachedReferences ??= scanReferences(productionSources());
  return cachedReferences;
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
  // Dead tier-naming gate copy the first orphan scan turned up.
  "contexts.quotaReached",
  "contexts.upgradePrompt",
  "contexts.upgradeToCreateMore",
  "contexts.upgradeToProShare",
  "workspace.proPlanBenefits",
  "workspace.proPlanRequiredTitle",
  "workspace.upgradeToPro",
  "workspace.seatWarning",
  "workspace.memberQuotaExceeded",
  "workspace.workspaceLimitReached",
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

/**
 * The catalogue key holding the product's own name for a gated feature: the
 * page title or nav entry of the surface the gate sits on. Features without
 * one exact-match term elsewhere in the catalogue are not listed.
 */
const PRODUCT_TERM: Readonly<Partial<Record<GateKey, string>>> = {
  resources: "sidebar.resources",
  connectors: "sidebar.connectors",
  shared_contexts: "workspace.planMatrix.row_sharedContexts",
  team_invitations: "workspace.planMatrix.row_teamInvitations",
  reranking: "searchSettings.reranking",
  memory_analysis: "analyses.header.title",
  managed_llm: "workspace.planMatrix.row_managedLlm",
  managed_embeddings: "workspace.planMatrix.row_managedEmbeddings",
  byok: "externalKeys.title",
  contexts: "contexts.title",
  members: "sidebar.members",
  resource_tokens: "resourceTokens.title",
  storage: "sidebar.storage",
  memories: "memories.title",
  api_calls: "workspace.apiCalls",
};

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

/** The orphans among `keys`, scanning `sources` (fixture code). */
function orphansIn(keys: readonly string[], ...sources: string[]): string[] {
  const refs = scanReferences(sources.map((code) => ({ code })));
  return keys.filter((key) => !isReferenced(key, refs));
}

describe("the orphan reference scan", () => {
  it("does not let a bare leaf in one namespace reference that leaf everywhere", () => {
    // The old scan's false negative: `t("title")` anywhere referenced every
    // key ending in `.title`.
    const unrelated = `
      const t = useTranslations("y");
      t("title");
    `;
    expect(orphansIn(["x.newThing.title", "y.title"], unrelated)).toEqual([
      "x.newThing.title",
    ]);
  });

  it("resolves nested namespaces and several translators in one file", () => {
    const code = `
      const t = useTranslations("a.b");
      const tCommon = useTranslations("common");
      t("c.d");
      tCommon("save");
    `;
    expect(
      orphansIn(["a.b.c.d", "common.save", "a.b.save", "common.c.d"], code),
    ).toEqual(["a.b.save", "common.c.d"]);
  });

  it("binds getTranslations and createTranslator, and the root translator", () => {
    const code = `
      const t = await getTranslations({ locale, namespace: "page" });
      const g = createTranslator({ locale, messages, namespace: "gate" });
      const r = await getTranslations("server");
      const root = useTranslations();
      t("title"); g("plan.title"); r("heading"); root("device.title");
    `;
    expect(
      orphansIn(
        [
          "page.title",
          "gate.plan.title",
          "server.heading",
          "device.title",
          "page.heading",
        ],
        code,
      ),
    ).toEqual(["page.heading"]);
  });

  it("counts t.rich, t.raw, t.markup and t.has, but not a call on an unbound name", () => {
    const code = `
      const t = useTranslations("n");
      t.rich("a", {}); t.raw("b"); t.markup("c", {}); t.has("d");
      other("e"); props.t("f");
    `;
    expect(orphansIn(["n.a", "n.b", "n.c", "n.d", "n.e", "n.f"], code)).toEqual(
      ["n.e", "n.f"],
    );
  });

  it("reads a runtime key as its static prefix, under its own namespace only", () => {
    const code = `
      const t = useTranslations("n");
      const tOther = useTranslations("o");
      t(\`plan.\${x}.title\`);
      tOther(key as any);
      t(flag ? "yes" : \`no.\${y}\`);
    `;
    expect(
      orphansIn(
        ["n.plan.a.title", "n.planb", "o.anything", "n.yes", "n.no.z", "n.x"],
        code,
      ),
    ).toEqual(["n.planb", "n.x"]);
  });

  it("ignores comments and string literals outside a translator call", () => {
    const code = `
      const t = useTranslations("n");
      // t("commented")
      /* t("blocked") */
      const label = "n.loose";
      t("real"); // t("trailing")
    `;
    expect(
      orphansIn(
        ["n.commented", "n.blocked", "n.loose", "n.real", "n.trailing"],
        code,
      ),
    ).toEqual(["n.commented", "n.blocked", "n.loose", "n.trailing"]);
  });

  it("takes the nearest binding before a call when one name is bound twice", () => {
    const code = `
      function A() { const t = useTranslations("a"); return t("one"); }
      function B() { const t = useTranslations("b"); return t("two"); }
    `;
    expect(orphansIn(["a.one", "b.two", "a.two", "b.one"], code)).toEqual([
      "a.two",
      "b.one",
    ]);
  });

  it("resolves a namespace passed as an identifier to its literal default", () => {
    const code = `
      function List({ translationNamespace = "admin.sleepReports" }) {
        const t = useTranslations(translationNamespace);
        return t("title");
      }
    `;
    expect(orphansIn(["admin.sleepReports.title", "x.title"], code)).toEqual([
      "x.title",
    ]);
  });

  it("sees a call inside a template expression and past a JSX closing tag", () => {
    const code = `
      const t = useTranslations("n");
      const s = \`\${t("inTemplate")} text\`;
      const el = <p><span>{t("a")}</span><span>{t("b")}</span></p>;
      const re = /"t\("c"\)"/;
    `;
    expect(orphansIn(["n.inTemplate", "n.a", "n.b", "n.c"], code)).toEqual([
      "n.c",
    ]);
  });

  it("a runtime key on the root translator stands for the file's own literals, not every key", () => {
    const code = `
      const MAP = { read: "device.scopeRead" };
      const t = useTranslations();
      t(MAP[scope]);
    `;
    expect(orphansIn(["device.scopeRead", "other.key"], code)).toEqual([
      "other.key",
    ]);
  });
});

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

  // 2
  it("every message key is referenced from src/, or allowlisted", () => {
    const refs = sourceReferences();
    const allowlisted = new Set(ORPHAN_ALLOWLIST);
    const orphans = leaves(en)
      .map(([key]) => key)
      .filter((key) => !isReferenced(key, refs) && !allowlisted.has(key));
    // Reference the key or delete it from both locales. Do not add it to
    // orphans.allowlist.ts — that baseline only shrinks.
    expect(orphans).toEqual([]);
  });

  // 3
  it("the orphan allowlist has no stale entries", () => {
    const refs = sourceReferences();
    const keys = new Set(leaves(en).map(([key]) => key));
    const stale = ORPHAN_ALLOWLIST.filter(
      (key) => !keys.has(key) || isReferenced(key, refs),
    );
    // Gone from en.json, or referenced again: remove the entry.
    expect(stale).toEqual([]);
    // Sorted, no duplicates — so a diff to it is always a plain removal.
    expect([...ORPHAN_ALLOWLIST]).toEqual(
      [...new Set(ORPHAN_ALLOWLIST)].sort(),
    );
  });

  // 3, narrowed: the baseline never shelters gate copy.
  it("the orphan allowlist holds no gate.* key and no superseded gate key", () => {
    const superseded = new Set(SUPERSEDED_GATE_KEYS);
    expect(
      ORPHAN_ALLOWLIST.filter(
        (key) => key.startsWith("gate.") || superseded.has(key),
      ),
    ).toEqual([]);
  });

  // 5, outside gate.*
  it("no superseded gate key survives outside gate.* (each named a tier or duplicated gate copy)", () => {
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

  // 8, widened: a gate names the feature the way the product already does —
  // the page or nav entry the gate sits on — not in a word of its own. ja has
  // no case, so every form must equal the term; en compares the sentence-case
  // label case-insensitively ("Resource tokens" = "Resource Tokens").
  it.each(Object.entries(PRODUCT_TERM))(
    "gate.features.%s names the feature as %s does",
    (feature, termKey) => {
      const term = messageAt(messages, termKey);
      expect(typeof term, termKey).toBe("string");
      const nouns = messages.gate.features[feature as GateKey];
      if (locale === "ja") {
        expect([nouns.label, nouns.plural, nouns.singular]).toEqual([
          term,
          term,
          term,
        ]);
      } else {
        expect(nouns.label.toLowerCase()).toBe((term as string).toLowerCase());
      }
    },
  );

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
