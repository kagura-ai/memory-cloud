/**
 * The gate descriptor (#1641 epic: #1644 + #1645).
 *
 * One vocabulary for "why can't I use this?". #1644 shipped the wire half —
 * the types, `normalizeGate` (called once, at the `ApiError` choke point in
 * `lib/api/base.ts`), `narrowCanUpgrade` and `gateFromFacts`. #1645 extends
 * this same module in place with the pre-check half: `GATE_SPECS` (whose keys
 * ARE the `GateKey` vocabulary), `resolveGate` against the tier matrix and
 * `/system/info`, `quotaGate` for page-local counts, and the matrix-aware
 * widening of `gateFromFacts`. The React bindings are `hooks/useFeatureGate.ts`
 * (pre-check) and `hooks/useErrorGate.ts` (refusal).
 *
 * Pure on purpose: no React, and NEVER an import from `@/lib/api/base` — not
 * even `import type { ApiError }`. `base.ts` imports `normalizeGate` from
 * here, so the reverse import would be a cycle. For the same reason the
 * matrix hook module is imported for its TYPES only. The `instanceof
 * ApiError` check lives in the React binding, `hooks/useErrorGate.ts`.
 */

import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { planLabelForTier } from "@/lib/utils/planLabel";
import type { SystemFeatures } from "@/lib/api/system";
import type { PlanTierFeature } from "@/lib/api/workspaces";
import type { PlanFeature } from "@/hooks/usePlanFeatures";
import type { WorkspaceObjectKind } from "@/hooks/useWorkspaceObjectPresence";

/** Compile-time assertions (no runtime cost). */
type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2
    ? true
    : false;
type Expect<T extends true> = T;

// ── States ──────────────────────────────────────────────────────────────────

/**
 * Why a feature is unavailable. Exactly one applies.
 *
 * The five terminal members are byte-identical to the server's `details.gate`
 * vocabulary (`backend/src/config/constants.py` `GATE_KINDS`), so the wire
 * value maps 1:1 onto the descriptor with no translation table.
 * "pending" and "allowed" are client-only and never appear on the wire.
 */
export type FeatureGateState =
  | "pending" // at least one required input is unresolved — NEVER upsell
  | "allowed" // may use it
  | "plan" // the workspace's tier does not include it
  | "quota" // included, but the cap is reached
  | "deployment" // an operator flag is off on this deployment
  | "role" // included, but this member's workspace role may not
  | "allowlist"; // included, but this workspace is not in the rollout

/** The subset a refusal can carry. */
export type RefusedGateState = Exclude<FeatureGateState, "pending" | "allowed">;

const REFUSED_GATE_STATES: readonly RefusedGateState[] = [
  "plan",
  "quota",
  "deployment",
  "role",
  "allowlist",
];

function isRefusedGateState(v: unknown): v is RefusedGateState {
  return (
    typeof v === "string" &&
    (REFUSED_GATE_STATES as readonly string[]).includes(v)
  );
}

/** True for the five refusal states. Exported so no call site writes the negation twice. */
export function isBlocked(g: { state: FeatureGateState }): boolean {
  return g.state !== "pending" && g.state !== "allowed";
}

// ── GATE_SPECS and the GateKey vocabulary ───────────────────────────────────

/** A `/system/info` feature flag a gate depends on. */
export interface GateFlag {
  readonly key: string;
  /**
   * What an ABSENT flag means. The polarity is NOT uniform across flags:
   * `false` = default-off, the Sidebar's rule (anything but `true` hides);
   * `true` = default-on, #1580's rule for the reranker card (hidden only on
   * an explicit `=== false`, so a backend predating the flag keeps it).
   */
  readonly whenAbsent: boolean;
}

/**
 * The numeric tier-row columns (`-?` + `NonNullable` keep the optional
 * `memories_per_day?` in). Used for quota caps, and — pinned to two of them
 * by `NumericGateField` — for the two plan gates a positive limit answers.
 */
type NumericPlanTierKey = {
  [K in keyof PlanTierFeature]-?: NonNullable<PlanTierFeature[K]> extends number
    ? K
    : never;
}[keyof PlanTierFeature];

/**
 * The numeric columns a PLAN gate may test with `kind: "positive"`. Pinned to
 * the two `GATE_SPECS` uses, so an unrelated cap (`max_contexts`,
 * `memory_limit`, …) cannot be wired as a plan gate by accident.
 */
export type NumericGateField = Extract<
  NumericPlanTierKey,
  "sleep_enabled_contexts_limit" | "analysis_runs_per_day"
>;
export type MatrixGateField = PlanFeature | NumericGateField;
type _NumericGateFieldsExist = Expect<
  Equal<
    NumericGateField,
    "sleep_enabled_contexts_limit" | "analysis_runs_per_day"
  >
>;

/**
 * The matrix test, typed so a boolean column cannot be paired with
 * "positive" or a numeric one with "boolean":
 *   "boolean":  tier[field] === true
 *   "positive": (tier[field] ?? 0) > 0
 */
export type MatrixGateTest =
  | { readonly field: PlanFeature; readonly kind: "boolean" }
  | { readonly field: NumericGateField; readonly kind: "positive" };

/** Where a gate's answer comes from. Every field is optional. */
export interface GateSpec {
  /** Tier-matrix source (`/plans/tiers`, the workspace's own row). */
  readonly matrix?: MatrixGateTest;
  /** `/system/info` flags. Every one must pass, each by its own polarity. */
  readonly flags?: readonly GateFlag[];
  /**
   * Minimum workspace role. Owner or Admin only — no gate notice is rendered
   * for a member-minimum gate (the Sidebar's Member entries are nav filters).
   */
  readonly role?: WorkspaceRole.Owner | WorkspaceRole.Admin;
  /**
   * What the UI does when the matrix test fails. "refuse" (the default) =
   * block. "degrade" = `allowed` + `degraded: true`: the control stays usable
   * and the server quietly does less.
   *
   * A deliberate LOCAL override, not a mirror of the server: since #1648
   * every `/plans/tiers` row serves `feature_enforcement`
   * (`FeatureEnforcementMode` in `lib/api/workspaces.ts`), and nothing here
   * reads it. Where the two disagree, that served map is the machine-readable
   * record of the divergence.
   */
  readonly enforcement?: "refuse" | "degrade";
  /**
   * "Gates block NEW, existing objects keep working" (#1551 / #1616).
   * Consumed by the Sidebar's nav filter only — never by `resolveGate`, which
   * would otherwise stop the pages showing their upsell for new objects.
   */
  readonly existingObjects?: WorkspaceObjectKind;
}

/**
 * Every gate, keyed by the one vocabulary of the whole epic: these keys are
 * the descriptor's `feature`, the `gate.features.<key>` i18n nouns (#1646)
 * and the pre-check spec keys. snake_case throughout — the server's own
 * spelling (`FEATURE_MIN_PLANS`), so the wire's `details.feature` needs no
 * translation table.
 *
 * `sleep_reports` and the quota keys are not server registry features
 * (`KNOWN_FEATURES`); they share the key space so every notice takes one
 * vocabulary. A test pins the matrix-bearing subset to the server's names.
 */
export const GATE_SPECS = {
  // ── plan create-gates already on the matrix (#1551 / #1560 / #1583) ──
  resources: {
    matrix: { field: "resources", kind: "boolean" },
    existingObjects: "resources",
  },
  connectors: {
    matrix: { field: "connectors", kind: "boolean" },
    existingObjects: "connectors",
  },
  public_contexts: { matrix: { field: "public_contexts", kind: "boolean" } },
  shared_contexts: { matrix: { field: "shared_contexts", kind: "boolean" } },

  // ── widened in #1645 ──
  team_invitations: {
    matrix: { field: "team_invitations", kind: "boolean" },
    role: WorkspaceRole.Admin,
  },
  reranking: {
    matrix: { field: "reranking", kind: "boolean" },
    // #1580 polarity: only an explicit `false` turns the reranker off.
    flags: [{ key: "reranking", whenAbsent: true }],
    // P-7 (open product decision): the UI hard-blocks the lowest tier, which
    // is today's behaviour. The server only degrades — #1648 has shipped the
    // mechanism, and `/plans/tiers` now serves `feature_enforcement.reranking
    // === "degrades"`, which is the machine-readable record of this
    // divergence. Flipping to "degrade" is this one line, once decided.
    enforcement: "refuse",
  },
  // `sleep_enabled_contexts_limit > 0` is the server's own sleep gate: the
  // zero floor makes the effective limit positive exactly when the tier's is.
  sleep_reports: {
    matrix: { field: "sleep_enabled_contexts_limit", kind: "positive" },
    role: WorkspaceRole.Admin,
  },
  memory_analysis: {
    matrix: { field: "analysis_runs_per_day", kind: "positive" },
    role: WorkspaceRole.Owner,
  },
  managed_llm: {
    matrix: { field: "managed_llm", kind: "boolean" },
    flags: [{ key: "managed_llm", whenAbsent: false }],
  },
  managed_embeddings: {
    matrix: { field: "managed_embeddings", kind: "boolean" },
  },
  secret_store: { matrix: { field: "secret_store", kind: "boolean" } },

  // ── deployment-only gates (no plan dimension) ──
  plan_page: {
    flags: [{ key: "plan_page", whenAbsent: false }],
    role: WorkspaceRole.Owner,
  },
  byok: {
    flags: [{ key: "byok", whenAbsent: false }],
    existingObjects: "externalKeys",
  },
  cost_dashboard: {
    flags: [
      { key: "byok", whenAbsent: false },
      { key: "cost_display", whenAbsent: false },
    ],
  },

  // ── quota-only keys: no pre-check source; quotaGate / the wire only ──
  contexts: {},
  members: {},
  workspaces: {},
  resource_tokens: {},
  storage: {},
  memories: {},
  agents: {},
  embedding_spend: {},
  api_calls: {},
} as const satisfies Record<string, GateSpec>;

export type GateKey = keyof typeof GATE_SPECS;

/** The runtime list of every `GateKey`, in `GATE_SPECS` order. */
export const GATE_KEYS = Object.keys(GATE_SPECS) as readonly GateKey[];
// The runtime list and the spec keys can never drift apart.
type _KeysMatch = Expect<
  Equal<(typeof GATE_KEYS)[number], keyof typeof GATE_SPECS>
>;

export function isGateKey(v: unknown): v is GateKey {
  return typeof v === "string" && (GATE_KEYS as readonly string[]).includes(v);
}

/**
 * `details.quota_type` → `GateKey`. The domain is exactly the server's frozen
 * `QUOTA_TYPES` (`backend/src/config/constants.py`); a test pins both the
 * domain and that the range stays inside `GATE_KEYS`.
 *
 * `workspace_limit_reached` reads like prose but is the wire value an older
 * client already matches on, so it is kept verbatim server-side.
 */
export const QUOTA_TYPE_TO_GATE_KEY: Readonly<Record<string, GateKey>> = {
  contexts: "contexts",
  members: "members",
  workspace_limit_reached: "workspaces",
  memories_per_day: "memories",
  memory_analysis: "memory_analysis",
  sleep_enabled_contexts: "sleep_reports",
  storage_bytes: "storage",
  agents: "agents",
  resource_tokens: "resource_tokens",
  connectors: "connectors",
  embedding_spend_daily: "embedding_spend",
  embedding_spend_monthly: "embedding_spend",
  api_mcp_daily: "api_calls",
  api_rest_daily: "api_calls",
  api_public_daily: "api_calls",
};

// ── The wire half ───────────────────────────────────────────────────────────

/**
 * What a refusal told us. Attached to `ApiError.gate` by `lib/api/base.ts`.
 *
 * Carries only what the server said: no `canUpgrade`, no resolved label, no
 * `GateKey` narrowing — none of the three is knowable at the transport layer
 * (`canUpgrade` needs `/system/info` and the member's role).
 *
 * Deliberately NOT narrowed: `feature`, `requiredPlan` and `currentPlan` stay
 * raw server strings, because `/plans/tiers` serves operator-defined tiers
 * the client's four-name `PlanTier` union has never heard of. Narrowing here
 * would silently drop an operator's tier key and turn a valid upgrade path
 * into "no tier has this feature".
 */
export interface FeatureGateFacts {
  readonly state: RefusedGateState;
  /** Registry feature key as the server spelled it. Absent on most quota refusals. */
  readonly feature?: string;
  /** Canonical snake_case quota key. Only ever set when state === "quota". */
  readonly quotaType?: string;
  /** Registry plan key that lifts the refusal. */
  readonly requiredPlan?: string;
  /** The server's `required_plan_display`. FALLBACK LABEL ONLY — see `resolvedPlanLabel`. */
  readonly requiredPlanLabel?: string;
  /** The workspace's plan key, when the server reported it. */
  readonly currentPlan?: string;
  /** Only ever set when state === "quota". */
  readonly current?: number;
  /** Only ever set when state === "quota". */
  readonly limit?: number;
  /** ISO-8601; time-windowed quotas only. */
  readonly resetsAt?: string;
}

// ── The UI descriptor ───────────────────────────────────────────────────────

/**
 * The single answer to "why can't I use this?", whatever asked the question:
 * a pre-check against the tier matrix (`resolveGate`), a page-local quota
 * (`quotaGate`) or a refusal the server just sent (`gateFromFacts`).
 *
 * Field presence is enforced by tests, not by the type: one flat interface
 * with optional fields, not a seven-arm discriminated union — the union would
 * force every producer to build seven literals and every `planLabel` read to
 * be narrowed first, for a shape whose consumer switches on `state` once.
 */
export interface FeatureGate {
  readonly state: FeatureGateState;

  /** Always present. The i18n noun key and the GATE_SPECS key. */
  readonly feature: GateKey;

  /**
   * Registry key of the tier that lifts a "plan" gate or raises a "quota"
   * cap. Absent on a "plan" gate exactly when NO served tier has the feature
   * (an operator override can strip it everywhere): never guessed.
   */
  readonly requiredPlan?: string;
  /** Resolved label for `requiredPlan`. Present exactly when requiredPlan is. */
  readonly planLabel?: string;

  /** The workspace's own tier. Needed by quota copy ("Your M plan allows 1 context"). */
  readonly currentPlan?: string;
  /** Resolved label for `currentPlan`. Present exactly when currentPlan is. */
  readonly currentPlanLabel?: string;

  /**
   * Quota numbers. Only ever set when state === "quota", and absent on the
   * two wire refusals that carry no counts: QUOTA-002 (USD floats under
   * other names) and the rate-limit 429 family.
   */
  readonly current?: number;
  readonly limit?: number;
  /** ISO-8601; time-windowed quotas only. */
  readonly resetsAt?: string;

  /**
   * Minimum workspace role. Only ever set when state === "role", from
   * `GATE_SPECS[feature].role` — on the pre-check path and on a wire role
   * gate alike (the wire never carries it: AUTH-101 details are stripped
   * server-side). Absent when the key's spec names no role.
   */
  readonly requiredRole?: WorkspaceRole.Owner | WorkspaceRole.Admin;

  /**
   * May an upgrade CTA be rendered here? Always a definite boolean, computed
   * in ONE place — `narrowCanUpgrade` below — by every producer. No consumer
   * re-derives it.
   */
  readonly canUpgrade: boolean;

  /**
   * The server DEGRADES rather than refusing (#1648). True only together
   * with state === "allowed", from a spec with `enforcement: "degrade"`; the
   * control stays usable. No spec is in degrade mode today (P-7).
   */
  readonly degraded?: boolean;
}

// ── Producer 0: the wire ────────────────────────────────────────────────────

/**
 * Legacy per-quota count names, for servers predating #1644, keyed by
 * `quota_type`. A pre-#1644 server therefore normalises to the same
 * `current` / `limit` a current one does. The canonical names win when both
 * are present (a current server sends both).
 */
const LEGACY_COUNT_FIELDS: Readonly<
  Record<string, { readonly current: string; readonly limit: string }>
> = {
  memory_analysis: { current: "used_today", limit: "limit_today" },
  memories_per_day: { current: "used_today", limit: "limit" },
  workspace_limit_reached: { current: "owned_count", limit: "cap" },
  connectors: { current: "active_connectors", limit: "max_connectors" },
};

function hasOwn(obj: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

function asNumber(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

function asString(v: unknown): string | undefined {
  return typeof v === "string" && v.length > 0 ? v : undefined;
}

/**
 * The refusal kind an older server implies through its SEMANTIC error code.
 * Never the status alone: a 403 or 429 with no recognised code is not a gate.
 * Status 402 is not mapped — nothing on the server emits it (#1641 P-4).
 *
 * `QUOTA-001` alone is not enough: the server also raises it for limits no
 * tier lifts (the 1 MB memory-size guard, the total memory cap, a missing
 * workspace), and those carry no `quota_type`. Only a `quota_type` from the
 * frozen vocabulary makes it a quota gate — the same rule the server applies
 * before it stamps `gate: "quota"`. Counts alone do not qualify: they name
 * no cap to render, and no server ever sent them without a type. Every
 * pre-#1644 quota refusal that carried numbers was typed, so an older server
 * still normalises identically; its untyped ones (the context cap, the daily
 * API quota) now read as no gate and keep their server message, which is
 * what their callers render when the gate carries no numbers anyway.
 * `QUOTA-002` and `CONNECTOR-001` name exactly one cap each, so the code is
 * enough for them.
 */
function stateFromErrorCode(
  status: number,
  errorCode: string | undefined,
  quotaType: string | undefined,
): RefusedGateState | undefined {
  switch (errorCode) {
    case "FEAT-001":
      return "plan";
    case "QUOTA-001":
      return quotaType !== undefined &&
        hasOwn(QUOTA_TYPE_TO_GATE_KEY, quotaType)
        ? "quota"
        : undefined;
    case "QUOTA-002":
    case "CONNECTOR-001":
      return "quota";
    case "AUTH-101":
      // The role refusal: its details are stripped server-side (CWE-639
      // defence in depth), so the code is the only signal there is.
      return status === 403 ? "role" : undefined;
    default:
      return undefined;
  }
}

/**
 * Derive the gate facts from an error body, once, at the transport choke
 * point. Returns `undefined` when the error is not a gate refusal — and
 * NEVER guesses from a bare status code.
 *
 * 1. `details.gate` wins when it is one of the five terminal states; an
 *    unknown value is ignored rather than trusted.
 * 2. Otherwise the semantic error code decides (an older server):
 *    FEAT-001 → plan; QUOTA-001 with a frozen `quota_type`, QUOTA-002 and
 *    CONNECTOR-001 → quota; AUTH-101 at 403 → role.
 * 3. Legacy count names are aliased onto `current` / `limit`, and a
 *    CONNECTOR-001 with no `quota_type` reads as `connectors`.
 */
export function normalizeGate(
  status: number,
  errorCode: string | undefined,
  details: Record<string, unknown> | undefined,
): FeatureGateFacts | undefined {
  const d: Record<string, unknown> = details ?? {};

  const state: RefusedGateState | undefined = isRefusedGateState(d.gate)
    ? d.gate
    : stateFromErrorCode(status, errorCode, asString(d.quota_type));
  if (!state) return undefined;

  const facts: {
    -readonly [K in keyof FeatureGateFacts]: FeatureGateFacts[K];
  } = { state };

  const feature = asString(d.feature);
  if (feature) facts.feature = feature;
  const requiredPlan = asString(d.required_plan);
  if (requiredPlan) facts.requiredPlan = requiredPlan;
  const requiredPlanLabel = asString(d.required_plan_display);
  if (requiredPlanLabel) facts.requiredPlanLabel = requiredPlanLabel;
  const currentPlan = asString(d.current_plan);
  if (currentPlan) facts.currentPlan = currentPlan;

  if (state === "quota") {
    const quotaType =
      asString(d.quota_type) ??
      (errorCode === "CONNECTOR-001" ? "connectors" : undefined);
    if (quotaType) facts.quotaType = quotaType;

    const legacy =
      quotaType && hasOwn(LEGACY_COUNT_FIELDS, quotaType)
        ? LEGACY_COUNT_FIELDS[quotaType]
        : undefined;
    const current =
      asNumber(d.current) ?? (legacy ? asNumber(d[legacy.current]) : undefined);
    if (current !== undefined) facts.current = current;
    const limit =
      asNumber(d.limit) ?? (legacy ? asNumber(d[legacy.limit]) : undefined);
    if (limit !== undefined) facts.limit = limit;

    const resetsAt = asString(d.resets_at);
    if (resetsAt) facts.resetsAt = resetsAt;
  }

  return facts;
}

// ── canUpgrade: the one narrowing ───────────────────────────────────────────

/**
 * The ONE place `canUpgrade` is narrowed by state. `raw` is
 * `canUpgradeFrom(...) === true` (hooks/useCanUpgrade.ts: the Plan page is
 * enabled on this deployment AND this member is the owner); callers pass it
 * through un-narrowed.
 *
 * - plan  → raw
 * - quota → raw, and only when a higher tier raises the cap
 * - every other state → false. Buying a tier does not turn on an operator's
 *   deployment flag, and allowlist copy must stay plan-neutral, so neither
 *   may ever carry an upgrade CTA; nor may a role gate.
 */
export function narrowCanUpgrade(
  state: FeatureGateState,
  requiredPlan: string | undefined,
  raw: boolean,
): boolean {
  if (state === "plan") return raw;
  if (state === "quota") return raw && requiredPlan !== undefined;
  return false;
}

// ── Labels and the matrix scan ──────────────────────────────────────────────

/**
 * The display label for a tier key, in one place for all three producers:
 *   1. a canonical OSS tier → the env-overridable label (`planLabelFromEnv`);
 *   2. else the matrix row's own `display_name`, when the matrix is loaded;
 *   3. else `serverLabel` (the refusal's `required_plan_display`);
 *   4. else the raw key.
 * `serverLabel` is undefined for the two pre-check producers, and for any
 * `currentPlan` (the wire ships no label for it).
 */
export function resolvedPlanLabel(
  name: string,
  tiers: readonly PlanTierFeature[] | null | undefined,
  serverLabel: string | undefined,
  locale: string | undefined,
): string {
  const matrixDisplayName = tiers?.find((t) => t.name === name)?.display_name;
  return planLabelForTier(name, matrixDisplayName ?? serverLabel, locale);
}

/**
 * The first served tier that passes `test`, or `null` when none does.
 *
 * Uses the array AS SERVED (the server's `PLAN_ORDER`, lowest first), not the
 * client's `PLAN_TIER_ORDER`: that is what lets a `PLAN_<KEY>_FEATURES`
 * operator override — or a tier the client has never heard of — flow through
 * with no frontend change. `null` mirrors the server's `required_plan: null`.
 */
export function requiredTierFor(
  tiers: readonly PlanTierFeature[],
  test: (tier: PlanTierFeature) => boolean,
): PlanTierFeature | null {
  return tiers.find(test) ?? null;
}

function matrixPredicate(
  test: MatrixGateTest,
): (tier: PlanTierFeature) => boolean {
  if (test.kind === "boolean") {
    const { field } = test;
    // `=== true`: a column an older API omits fails closed.
    return (tier) => tier[field] === true;
  }
  const { field } = test;
  return (tier) => (tier[field] ?? 0) > 0;
}

/**
 * The matrix column holding each quota's cap, for "which tier raises it?".
 * Exactly the caps the server itself derives an upgrade tier for
 * (`lowest_tier_with_limit`, #1644). `storage` and `agents` are absent on
 * purpose — neither has an upgrade path (P-6) — so a quota gate on them
 * never carries `requiredPlan`, and therefore never a CTA.
 */
const QUOTA_CAP_FIELDS: Partial<Record<GateKey, NumericPlanTierKey>> = {
  contexts: "max_contexts",
  members: "max_members",
  resource_tokens: "max_resource_tokens",
  connectors: "max_connectors",
  memories: "memories_per_day",
  memory_analysis: "analysis_runs_per_day",
  sleep_reports: "sleep_enabled_contexts_limit",
};

/** The lowest served tier whose cap for `key` is above `limit`. */
function tierRaisingCap(
  key: GateKey,
  limit: number,
  tiers: readonly PlanTierFeature[] | null | undefined,
): string | undefined {
  const field = hasOwn(QUOTA_CAP_FIELDS, key)
    ? QUOTA_CAP_FIELDS[key]
    : undefined;
  if (!field || !tiers) return undefined;
  return requiredTierFor(tiers, (t) => (t[field] ?? 0) > limit)?.name;
}

/** The lowest served tier whose row passes `key`'s plan test. */
function tierWithFeature(
  key: GateKey,
  tiers: readonly PlanTierFeature[] | null | undefined,
): string | undefined {
  const spec: GateSpec = GATE_SPECS[key];
  if (!spec.matrix || !tiers) return undefined;
  return requiredTierFor(tiers, matrixPredicate(spec.matrix))?.name;
}

/** `requiredPlan` / `currentPlan` and their resolved labels, when known. */
function tierFields(
  requiredPlan: string | undefined,
  currentPlan: string | undefined,
  tiers: readonly PlanTierFeature[] | null | undefined,
  serverLabel: string | undefined,
  locale: string | undefined,
): Pick<
  FeatureGate,
  "requiredPlan" | "planLabel" | "currentPlan" | "currentPlanLabel"
> {
  return {
    ...(requiredPlan !== undefined && {
      requiredPlan,
      planLabel: resolvedPlanLabel(requiredPlan, tiers, serverLabel, locale),
    }),
    ...(currentPlan !== undefined && {
      currentPlan,
      currentPlanLabel: resolvedPlanLabel(
        currentPlan,
        tiers,
        undefined,
        locale,
      ),
    }),
  };
}

// ── Producer C: lift a refusal to the UI descriptor ─────────────────────────

/**
 * `facts.feature` if it is a `GateKey`; else the quota-type map; else the
 * caller's `fallbackKey`. Never throws, so every descriptor names a feature.
 */
function resolveGateKey(
  facts: FeatureGateFacts,
  fallbackKey: GateKey,
): GateKey {
  if (isGateKey(facts.feature)) return facts.feature;
  if (facts.quotaType && hasOwn(QUOTA_TYPE_TO_GATE_KEY, facts.quotaType)) {
    return QUOTA_TYPE_TO_GATE_KEY[facts.quotaType];
  }
  return fallbackKey;
}

/**
 * Lift a server refusal to the UI descriptor. Takes FACTS, not an ApiError,
 * so this module never imports `lib/api/base`.
 *
 * Returns `null` when `facts` is undefined, so a caller with its own handling
 * for non-gate errors keeps it.
 *
 * `ctx.canUpgrade` is the RAW `canUpgradeFrom(...) === true`; it is narrowed
 * here by `narrowCanUpgrade`, so no caller re-derives it. Field presence
 * follows the state: plan / required tier only on plan and quota gates,
 * counts only on quota gates, `requiredRole` only on role gates, nothing but
 * the feature on the rest.
 *
 * With `ctx.tiers` (#1645) the matrix is consulted too: tier labels resolve
 * through `resolvedPlanLabel` (the refusal's own `required_plan_display` is
 * the fallback), and a refusal that names no tier — a server predating #1644
 * — gets the same matrix scan the pre-check uses, so both paths name the
 * same tier. A role gate takes its minimum role from `GATE_SPECS`, since the
 * wire never carries one.
 */
export function gateFromFacts(
  facts: FeatureGateFacts | undefined,
  ctx: {
    /** What the caller was trying to do. Used when the wire names no feature. */
    fallbackKey: GateKey;
    /** Raw: `canUpgradeFrom(...) === true`. Narrowed inside. */
    canUpgrade: boolean;
    locale: string | undefined;
    /** The shared tier matrix, when the caller holds it. */
    tiers?: readonly PlanTierFeature[] | null;
  },
): FeatureGate | null {
  if (!facts) return null;

  const { state } = facts;
  const feature = resolveGateKey(facts, ctx.fallbackKey);
  const tiers = ctx.tiers ?? null;
  const spec: GateSpec = GATE_SPECS[feature];

  let requiredPlan: string | undefined;
  if (state === "plan") {
    requiredPlan = facts.requiredPlan ?? tierWithFeature(feature, tiers);
  } else if (state === "quota") {
    requiredPlan =
      facts.requiredPlan ??
      (facts.limit !== undefined
        ? tierRaisingCap(feature, facts.limit, tiers)
        : undefined);
  }
  const currentPlan =
    state === "plan" || state === "quota" ? facts.currentPlan : undefined;
  // The server's label belongs to the server's tier, not to a matrix guess.
  const serverLabel =
    facts.requiredPlan !== undefined ? facts.requiredPlanLabel : undefined;
  const requiredRole = state === "role" ? spec.role : undefined;

  return {
    state,
    feature,
    ...tierFields(requiredPlan, currentPlan, tiers, serverLabel, ctx.locale),
    ...(state === "quota" &&
      facts.current !== undefined && {
        current: facts.current,
      }),
    ...(state === "quota" &&
      facts.limit !== undefined && {
        limit: facts.limit,
      }),
    ...(state === "quota" &&
      facts.resetsAt !== undefined && {
        resetsAt: facts.resetsAt,
      }),
    ...(requiredRole !== undefined && { requiredRole }),
    canUpgrade: narrowCanUpgrade(state, requiredPlan, ctx.canUpgrade),
  };
}

// ── Producer A: the pre-check ───────────────────────────────────────────────

/** A descriptor that carries nothing but its state and feature. */
function bareGate(
  state: "pending" | "allowed" | "deployment",
  feature: GateKey,
): FeatureGate {
  return { state, feature, canUpgrade: false };
}

/**
 * Is `flag` resolved OFF? Each flag by its own polarity: a default-on flag
 * (`whenAbsent: true`) is off only on an explicit `false`; a default-off flag
 * is off on anything but `true`.
 */
function isFlagOff(features: SystemFeatures, flag: GateFlag): boolean {
  const value = features[flag.key];
  return flag.whenAbsent ? value === false : value !== true;
}

function quotaDescriptor(args: {
  key: GateKey;
  current: number;
  limit: number;
  planName: string | null | undefined;
  tiers: readonly PlanTierFeature[] | null;
  canUpgrade: boolean;
  locale: string | undefined;
}): FeatureGate {
  const requiredPlan = tierRaisingCap(args.key, args.limit, args.tiers);
  return {
    state: "quota",
    feature: args.key,
    ...tierFields(
      requiredPlan,
      args.planName || undefined,
      args.tiers,
      undefined,
      args.locale,
    ),
    current: args.current,
    limit: args.limit,
    canUpgrade: narrowCanUpgrade("quota", requiredPlan, args.canUpgrade),
  };
}

/**
 * Answer a gate before the user tries — from the tier matrix, `/system/info`
 * and the member's role. Pure; `useFeatureGate` feeds it resolved inputs.
 *
 * ── The two failure directions, documented once, here ──
 *
 * The two inputs fail in OPPOSITE directions, deliberately, and nothing may
 * "unify" them:
 *
 * - A tier-matrix transport failure stays PENDING. `usePlanTierMatrix`
 *   answers `null` both while fetching and after its three attempts (the
 *   failure is not cached, so a later mount retries). A matrix gate is then
 *   `pending` — never `plan`, never a `requiredPlan` guess — because a
 *   pending gate must never flash an upsell at an entitled tenant.
 * - A `/system/info` transport failure falls CLOSED. After its three
 *   attempts `useSystemFeatures` resolves `{}`, so every flag reads absent.
 *   That is right for both of its consumers: a withheld CTA costs a click,
 *   a dead-ending CTA costs trust, and a deployment notice is definitive.
 *
 * The one precedence ruling that keeps both intact: once `/system/info` has
 * RESOLVED — the failed-closed `{}` included — a flag that is off outranks a
 * still-pending matrix, so the user gets the CTA-free "not available on this
 * deployment" answer instead of an endless spinner. While `/system/info` is
 * itself in flight (`features === null`) no flag has resolved, so the gate is
 * `pending`, never `deployment`. A spec with no `flags` therefore never
 * reaches `deployment`, and every plan gate keeps exactly the pending
 * semantics above. Flag polarity is per flag (`GateFlag.whenAbsent`): the
 * reranker is default-ON (#1580), every other flag default-OFF. Caveat, not
 * changed here: a failed `/system/info` and a backend that never had the
 * flag are indistinguishable — both read absent.
 *
 * `workspaceResolved` is `currentWorkspace !== null` — byte-identical to
 * `usePlanFeatures` — and deliberately NOT `!loading`: a user with no
 * workspace has `loading === false` and no workspace forever. `!loading`
 * would fail the matrix test closed on an undefined plan and upsell someone
 * with nothing to upgrade; this way they stay `pending`.
 *
 * ── Precedence (fixed order, first hit wins) ──
 *
 *   1. deployment — `features` resolved and some spec flag is off
 *   2. pending    — a required input is unresolved: `features` (spec has
 *                   flags), the matrix (spec has one), the workspace (spec
 *                   has a matrix or a role)
 *   3. role       — `spec.role` set and the member's role is below it. Above
 *                   plan: a member cannot buy their way to owner, so an
 *                   upsell to them would be a lie.
 *   4. plan       — the workspace's row fails the matrix test (an unknown
 *                   plan fails closed); "degrade" specs answer `allowed` +
 *                   `degraded` instead. `requiredPlan` is the first served
 *                   tier that passes, absent when none does.
 *   5. quota      — `quota` supplied, `limit > 0` and `current >= limit`
 *   6. allowed
 *
 * The server checks owner → feature → quota → allowlist
 * (`backend/src/auth/analysis_gates.py`). There is NO allowlist step and NO
 * `allowlisted` input: nothing in the client can supply one — allowlist
 * membership is consulted only inside the server gate — so the client meets
 * "allowlist" only on the wire, through `gateFromFacts`. When a surface can
 * supply it, the step goes BELOW role, matching the server's order.
 *
 * ── The pending truth table (tested row by row) ──
 *
 *   #   tiers  workspace             features        spec flags       result
 *   1   null   any                   any             none             pending
 *   2   array  unresolved            any             none             pending
 *   3   array  resolved, test true   any             none             allowed
 *   4   array  resolved, test false  any             none             plan
 *   5   any    any                   null            some             pending
 *   6   any    any                   {} (failed)     whenAbsent:false deployment
 *   7   any    any                   {} (failed)     whenAbsent:true  rows 1-4
 *   8   any    any                   flag === false  either polarity  deployment
 *   9   null   any                   flag === true   some             pending
 *   10  array  resolved, test false  flag === true   some             plan
 *   11  array  resolved, test true   flag === true   some             allowed
 *   12  array  none (not loading)    any             any              pending
 *
 * `canUpgrade` is the raw `canUpgradeFrom(...) === true`, narrowed here by
 * `narrowCanUpgrade` like every producer: a deployment, role, pending or
 * allowed gate never offers an upgrade. `GateSpec.existingObjects` is never
 * read here (it is the Sidebar's rule, not a gate answer).
 */
export function resolveGate(input: {
  key: GateKey;
  /** null = unresolved OR failed (indistinguishable, by design). */
  tiers: readonly PlanTierFeature[] | null;
  planName: string | null | undefined;
  /** `currentWorkspace !== null`. NOT `!loading`. */
  workspaceResolved: boolean;
  /** null = unresolved; `{}` = failed closed. */
  features: SystemFeatures | null;
  role: string | null | undefined;
  /** Raw: `canUpgradeFrom(...) === true`. Narrowed inside. */
  canUpgrade: boolean;
  locale: string | undefined;
  quota?: { current: number; limit: number };
}): FeatureGate {
  const { key, tiers, features } = input;
  const spec: GateSpec = GATE_SPECS[key];
  const flags = spec.flags ?? [];

  // 1. deployment — only once /system/info has resolved.
  if (features !== null && flags.some((flag) => isFlagOff(features, flag))) {
    return bareGate("deployment", key);
  }

  // 2. pending — never an upsell while an input is unresolved.
  if (flags.length > 0 && features === null) return bareGate("pending", key);
  if (spec.matrix && tiers === null) return bareGate("pending", key);
  if ((spec.matrix || spec.role) && !input.workspaceResolved) {
    return bareGate("pending", key);
  }

  // 3. role
  if (spec.role && !hasWorkspaceRole(input.role, spec.role)) {
    return {
      state: "role",
      feature: key,
      requiredRole: spec.role,
      canUpgrade: false,
    };
  }

  // 4. plan
  if (spec.matrix && tiers !== null) {
    const test = matrixPredicate(spec.matrix);
    const row = tiers.find((t) => t.name === input.planName);
    if (!row || !test(row)) {
      if (spec.enforcement === "degrade") {
        return {
          state: "allowed",
          feature: key,
          canUpgrade: false,
          degraded: true,
        };
      }
      const requiredPlan = requiredTierFor(tiers, test)?.name;
      return {
        state: "plan",
        feature: key,
        ...tierFields(
          requiredPlan,
          input.planName || undefined,
          tiers,
          undefined,
          input.locale,
        ),
        canUpgrade: narrowCanUpgrade("plan", requiredPlan, input.canUpgrade),
      };
    }
  }

  // 5. quota — only once the feature itself is allowed.
  const { quota } = input;
  if (quota && quota.limit > 0 && quota.current >= quota.limit) {
    return quotaDescriptor({
      key,
      current: quota.current,
      limit: quota.limit,
      planName: input.planName,
      tiers,
      canUpgrade: input.canUpgrade,
      locale: input.locale,
    });
  }

  // 6. allowed
  return bareGate("allowed", key);
}

// ── Producer B: page-local quota numbers ────────────────────────────────────

/**
 * A quota gate from counts the page already holds — no server round trip.
 *
 * `limit` 0 never blocks: callers pass 0 for an unknown cap, and "unknown"
 * must not block (the create call stays authoritative). `requiredPlan` is
 * the first served tier whose cap for `key` exceeds `limit`, absent when
 * none does or the matrix is unresolved — which is what keeps the storage
 * and agents caps, and a still-loading matrix, from offering an upgrade.
 */
export function quotaGate(args: {
  key: GateKey;
  current: number;
  limit: number;
  planName: string | null | undefined;
  tiers: readonly PlanTierFeature[] | null;
  /** Raw: `canUpgradeFrom(...) === true`. Narrowed inside. */
  canUpgrade: boolean;
  locale: string | undefined;
}): FeatureGate {
  if (!(args.limit > 0 && args.current >= args.limit)) {
    return bareGate("allowed", args.key);
  }
  return quotaDescriptor(args);
}
