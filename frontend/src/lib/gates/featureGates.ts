/**
 * The gate descriptor (#1641 epic; this half is #1644).
 *
 * One vocabulary for "why can't I use this?". #1644 ships the wire half —
 * the types, the `GateKey` vocabulary, `normalizeGate` (called once, at the
 * `ApiError` choke point in `lib/api/base.ts`), `narrowCanUpgrade` and
 * `gateFromFacts`. #1645 extends this same module in place with the tier
 * matrix pre-check (`GATE_SPECS`, `resolveGate`, `quotaGate`).
 *
 * Pure on purpose: no React, and NEVER an import from `@/lib/api/base` — not
 * even `import type { ApiError }`. `base.ts` imports `normalizeGate` from
 * here, so the reverse import would be a cycle. The `instanceof ApiError`
 * check lives in the React binding, `hooks/useErrorGate.ts`.
 */

import { isPlanTier, planLabelFromEnv } from "@/lib/utils/planLabel";

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

// ── The GateKey vocabulary ──────────────────────────────────────────────────

/**
 * Every feature a gate can name: the descriptor's `feature`, the future
 * `gate.features.<key>` i18n noun, and (from #1645) the `GATE_SPECS` key.
 * snake_case throughout — the server's own spelling, so the wire's
 * `details.feature` needs no translation table.
 *
 * #1644 ships the runtime array and the type; #1645 replaces the array with
 * `keyof typeof GATE_SPECS` and asserts the two are equal, so they can never
 * drift.
 */
export const GATE_KEYS = [
  "resources",
  "connectors",
  "public_contexts",
  "shared_contexts",
  "team_invitations",
  "reranking",
  "sleep_reports",
  "memory_analysis",
  "managed_llm",
  "managed_embeddings",
  "secret_store",
  "plan_page",
  "byok",
  "cost_dashboard",
  "contexts",
  "members",
  "workspaces",
  "resource_tokens",
  "storage",
  "memories",
  "agents",
  "embedding_spend",
  "api_calls",
] as const;

export type GateKey = (typeof GATE_KEYS)[number];

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
  /** The server's `required_plan_display`. FALLBACK LABEL ONLY — see `planLabelFor`. */
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
 * The single answer to "why can't I use this?", whatever asked the question.
 * In #1644 the only producer is `gateFromFacts` (a refusal the server just
 * sent); #1645 adds the pre-check producers.
 *
 * Field presence is enforced by tests, not by the type: one flat interface
 * with optional fields, not a seven-arm discriminated union — the union would
 * force every producer to build seven literals and every `planLabel` read to
 * be narrowed first, for a shape whose consumer switches on `state` once.
 *
 * `requiredRole` is not here yet: the wire never carries it (the server
 * strips AUTH-101 details), and #1645 adds it together with the `GATE_SPECS`
 * role it is read from.
 */
export interface FeatureGate {
  readonly state: FeatureGateState;

  /** Always present. The i18n noun key (and, from #1645, the GATE_SPECS key). */
  readonly feature: GateKey;

  /** Registry key of the tier that lifts a "plan" gate or raises a "quota" cap. */
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
   * May an upgrade CTA be rendered here? Always a definite boolean, computed
   * in ONE place — `narrowCanUpgrade` below — by every producer. No consumer
   * re-derives it.
   */
  readonly canUpgrade: boolean;

  /**
   * #1648 hook: the server DEGRADES rather than refusing. True only together
   * with state === "allowed". No #1644 producer sets it.
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

// ── Producer C: lift a refusal to the UI descriptor ─────────────────────────

/**
 * Display label for a tier. The four canonical OSS tiers go through the
 * env-overridable resolution (`NEXT_PUBLIC_PLAN_DISPLAY_NAMES`, default
 * S / M / L / XL); an operator-defined tier falls back to the server's
 * `required_plan_display`, then to the raw key. #1645 inserts the tier
 * matrix's own `display_name` between the two.
 */
function planLabelFor(
  name: string,
  serverLabel: string | undefined,
  locale: string | undefined,
): string {
  return isPlanTier(name)
    ? planLabelFromEnv(name, locale)
    : (serverLabel ?? name);
}

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
 * counts only on quota gates, nothing but the feature on the rest.
 */
export function gateFromFacts(
  facts: FeatureGateFacts | undefined,
  ctx: {
    /** What the caller was trying to do. Used when the wire names no feature. */
    fallbackKey: GateKey;
    /** Raw: `canUpgradeFrom(...) === true`. Narrowed inside. */
    canUpgrade: boolean;
    locale: string | undefined;
  },
): FeatureGate | null {
  if (!facts) return null;

  const { state } = facts;
  const tiered = state === "plan" || state === "quota";
  const requiredPlan = tiered ? facts.requiredPlan : undefined;
  const currentPlan = tiered ? facts.currentPlan : undefined;

  return {
    state,
    feature: resolveGateKey(facts, ctx.fallbackKey),
    ...(requiredPlan !== undefined && {
      requiredPlan,
      planLabel: planLabelFor(
        requiredPlan,
        facts.requiredPlanLabel,
        ctx.locale,
      ),
    }),
    ...(currentPlan !== undefined && {
      currentPlan,
      currentPlanLabel: planLabelFor(currentPlan, undefined, ctx.locale),
    }),
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
    canUpgrade: narrowCanUpgrade(state, requiredPlan, ctx.canUpgrade),
  };
}
