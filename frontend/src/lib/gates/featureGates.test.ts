/**
 * Tests for the gate descriptor (#1644 wire half, #1645 pre-check half).
 *
 * `normalizeGate` is the one place a refusal body becomes gate facts, so its
 * table is pinned here case by case — above all "never infer a gate from a
 * bare 403/429" and the legacy count aliases an older server still sends.
 * `resolveGate` is pinned by its pending truth table, row by row, and by the
 * precedence the two failure directions depend on.
 */

import { afterEach, describe, expect, it } from "vitest";

import { WorkspaceRole } from "@/lib/auth/rbac";
import type {
  FeatureEnforcementMode,
  PlanTierFeature,
} from "@/lib/api/workspaces";
import type { SystemFeatures } from "@/lib/api/system";

import {
  GATE_KEYS,
  GATE_SPECS,
  QUOTA_TYPE_TO_GATE_KEY,
  gateFromFacts,
  isBlocked,
  isGateKey,
  narrowCanUpgrade,
  normalizeGate,
  quotaGate,
  requiredTierFor,
  resolveGate,
  resolvedPlanLabel,
  type FeatureGateFacts,
  type FeatureGateState,
  type GateKey,
  type RefusedGateState,
} from "./featureGates";

/**
 * Copied from `backend/src/config/constants.py` `QUOTA_TYPES`. If the server
 * freezes a new quota type, this fixture and `QUOTA_TYPE_TO_GATE_KEY` move
 * together.
 */
const SERVER_QUOTA_TYPES = [
  "contexts",
  "members",
  "workspace_limit_reached",
  "memories_per_day",
  "memory_analysis",
  "sleep_enabled_contexts",
  "storage_bytes",
  "agents",
  "resource_tokens",
  "connectors",
  "embedding_spend_daily",
  "embedding_spend_monthly",
  "api_mcp_daily",
  "api_rest_daily",
  "api_public_daily",
];

/** Copied from `backend/src/config/constants.py` `GATE_KINDS`. */
const SERVER_GATE_KINDS: RefusedGateState[] = [
  "plan",
  "quota",
  "deployment",
  "role",
  "allowlist",
];

describe("normalizeGate — not a gate", () => {
  it("returns undefined for an error that is not a gate refusal", () => {
    expect(
      normalizeGate(404, "RES-001", { detail: "Context not found" }),
    ).toBeUndefined();
    expect(normalizeGate(500, undefined, undefined)).toBeUndefined();
  });

  it("never infers a gate from a bare 403", () => {
    expect(normalizeGate(403, undefined, undefined)).toBeUndefined();
    expect(normalizeGate(403, undefined, {})).toBeUndefined();
    // A raw HTTPException(403) reshaped by the global handler: the HTTP-*
    // placeholder is not a semantic code, and the prose is not a signal.
    expect(
      normalizeGate(403, "HTTP-403", {
        detail: "Team invitations require the L plan.",
      }),
    ).toBeUndefined();
  });

  it("never infers a gate from a bare 429", () => {
    expect(normalizeGate(429, undefined, undefined)).toBeUndefined();
    expect(
      normalizeGate(429, "HTTP-429", { detail: "Too many requests" }),
    ).toBeUndefined();
    expect(normalizeGate(429, "RATE-001", { retry_after: 60 })).toBeUndefined();
  });

  it("does not map status 402", () => {
    expect(normalizeGate(402, undefined, undefined)).toBeUndefined();
    expect(normalizeGate(402, "HTTP-402", {})).toBeUndefined();
  });

  it("treats AUTH-101 as a role gate only at 403", () => {
    expect(normalizeGate(401, "AUTH-101", {})).toBeUndefined();
  });

  it("ignores an unknown gate kind rather than trusting it", () => {
    expect(
      normalizeGate(403, "HTTP-403", { gate: "enterprise_only" }),
    ).toBeUndefined();
    expect(normalizeGate(403, undefined, { gate: "pending" })).toBeUndefined();
    expect(normalizeGate(403, undefined, { gate: "allowed" })).toBeUndefined();
    // ...and falls back to the semantic code when there is one.
    expect(
      normalizeGate(403, "FEAT-001", { gate: "Plan", feature: "connectors" }),
    ).toEqual({ state: "plan", feature: "connectors" });
  });
});

describe("normalizeGate — details.gate", () => {
  it.each(SERVER_GATE_KINDS)(
    "reads details.gate %s when the server states it",
    (gate) => {
      expect(normalizeGate(403, "FEAT-001", { gate })?.state).toBe(gate);
    },
  );

  it("prefers details.gate over the error code", () => {
    // The analysis allowlist refusal is FEAT-001 on the wire — wire-identical
    // to a plan refusal except for this annotation.
    expect(
      normalizeGate(403, "FEAT-001", {
        gate: "allowlist",
        feature: "memory_analysis",
        required_plan: null,
        required_plan_display: null,
        current_plan: "pro",
      }),
    ).toEqual({
      state: "allowlist",
      feature: "memory_analysis",
      currentPlan: "pro",
    });
  });

  it("honours details.gate on a refusal whose code did not move", () => {
    // The managed-LLM deployment refusal keeps its VAL-001 code.
    expect(
      normalizeGate(422, "VAL-001", {
        gate: "deployment",
        feature: "managed_llm",
      }),
    ).toEqual({ state: "deployment", feature: "managed_llm" });
  });

  it("reads the full FEAT-001 plan block", () => {
    expect(
      normalizeGate(403, "FEAT-001", {
        gate: "plan",
        feature: "team_invitations",
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "basic",
      }),
    ).toEqual({
      state: "plan",
      feature: "team_invitations",
      requiredPlan: "pro",
      requiredPlanLabel: "L",
      currentPlan: "basic",
    });
  });

  it("reads the full QUOTA-001 block", () => {
    expect(
      normalizeGate(429, "QUOTA-001", {
        gate: "quota",
        quota_type: "contexts",
        current: 1,
        limit: 1,
        required_plan: "basic",
        required_plan_display: "M",
        current_plan: "free",
        feature: null,
        resets_at: null,
      }),
    ).toEqual({
      state: "quota",
      quotaType: "contexts",
      current: 1,
      limit: 1,
      requiredPlan: "basic",
      requiredPlanLabel: "M",
      currentPlan: "free",
    });
  });
});

describe("normalizeGate — older servers (semantic code only)", () => {
  it("maps FEAT-001 to plan on a server predating #1644", () => {
    expect(
      normalizeGate(403, "FEAT-001", { feature: "memory_analysis" }),
    ).toEqual({ state: "plan", feature: "memory_analysis" });
  });

  it("maps a typed QUOTA-001 to quota", () => {
    expect(
      normalizeGate(429, "QUOTA-001", { quota_type: "workspace_limit_reached" }),
    ).toEqual({ state: "quota", quotaType: "workspace_limit_reached" });
  });

  it("maps QUOTA-002 to quota on the code alone (it names one cap)", () => {
    expect(normalizeGate(429, "QUOTA-002", {})).toEqual({ state: "quota" });
  });

  it("maps AUTH-101 403 to role with no other fields (details are stripped server-side)", () => {
    expect(normalizeGate(403, "AUTH-101", {})).toEqual({ state: "role" });
    expect(normalizeGate(403, "AUTH-101", undefined)).toEqual({
      state: "role",
    });
  });

  it("maps CONNECTOR-001 to quota with quotaType connectors", () => {
    expect(
      normalizeGate(403, "CONNECTOR-001", {
        max_connectors: 3,
        active_connectors: 3,
      }),
    ).toEqual({
      state: "quota",
      quotaType: "connectors",
      current: 3,
      limit: 3,
    });
  });
});

describe("normalizeGate — QUOTA-001 limits that are not gates", () => {
  // The server raises QUOTA-001 for the 1 MB memory-size guard, the total
  // memory cap and a missing workspace too. None of them is a plan quota,
  // so none may normalise to a quota gate — an upgrade treatment on a
  // request-size limit would promise something no tier provides.

  it("yields no gate for the untyped body the server sends", () => {
    // Byte-for-byte the details block backend/tests/api/
    // test_gate_error_contract.py pins for every NOT_GATE_REFUSALS site.
    expect(
      normalizeGate(429, "QUOTA-001", { quota_type: null }),
    ).toBeUndefined();
  });

  it("yields no gate for a QUOTA-001 with no details at all", () => {
    expect(normalizeGate(429, "QUOTA-001", {})).toBeUndefined();
    expect(normalizeGate(429, "QUOTA-001", undefined)).toBeUndefined();
  });

  it("does not accept counts in place of a quota_type", () => {
    // No server ever sent a count pair without a type, and a pair with no
    // type names no cap a client could render it against.
    expect(
      normalizeGate(429, "QUOTA-001", { current: 1, limit: 1 }),
    ).toBeUndefined();
  });

  it("yields no gate for a quota_type outside the frozen vocabulary", () => {
    // Mirrors the server, which stamps gate only for a QUOTA_TYPES member.
    expect(
      normalizeGate(429, "QUOTA-001", { quota_type: "not_a_frozen_type" }),
    ).toBeUndefined();
  });

  it("yields no gate for a pre-#1644 daily API quota body", () => {
    // The old rate-limit middleware replaced the details with retry_after.
    expect(
      normalizeGate(429, "QUOTA-001", { retry_after: 86400 }),
    ).toBeUndefined();
  });

  it("still trusts an explicit gate over the code rule", () => {
    // Rule 1: a server that says "quota" is believed; the type requirement
    // only governs the fallback for a server that says nothing.
    expect(normalizeGate(429, "QUOTA-001", { gate: "quota" })).toEqual({
      state: "quota",
    });
  });
});

describe("normalizeGate — legacy count aliases", () => {
  it("aliases used_today/limit_today onto current/limit for memory_analysis", () => {
    expect(
      normalizeGate(429, "QUOTA-001", {
        quota_type: "memory_analysis",
        used_today: 3,
        limit_today: 3,
        addon_bonus: 0,
        remaining_today: 0,
        resets_at: "2026-09-24T00:00:00+09:00",
      }),
    ).toEqual({
      state: "quota",
      quotaType: "memory_analysis",
      current: 3,
      limit: 3,
      resetsAt: "2026-09-24T00:00:00+09:00",
    });
  });

  it("aliases used_today/limit onto current/limit for memories_per_day", () => {
    expect(
      normalizeGate(429, "QUOTA-001", {
        quota_type: "memories_per_day",
        used_today: 100,
        limit: 100,
        requested: 1,
      }),
    ).toMatchObject({ current: 100, limit: 100 });
  });

  it("aliases owned_count/cap onto current/limit for workspace_limit_reached", () => {
    expect(
      normalizeGate(429, "QUOTA-001", {
        quota_type: "workspace_limit_reached",
        owned_count: 2,
        cap: 2,
        tier: "free",
        next_tier: "basic",
      }),
    ).toEqual({
      state: "quota",
      quotaType: "workspace_limit_reached",
      current: 2,
      limit: 2,
    });
  });

  it("aliases active_connectors/max_connectors onto current/limit", () => {
    expect(
      normalizeGate(403, "CONNECTOR-001", {
        quota_type: "connectors",
        max_connectors: 5,
        active_connectors: 4,
      }),
    ).toMatchObject({ current: 4, limit: 5 });
  });

  it("prefers the canonical names when both are present", () => {
    expect(
      normalizeGate(429, "QUOTA-001", {
        gate: "quota",
        quota_type: "memory_analysis",
        current: 7,
        limit: 8,
        used_today: 1,
        limit_today: 2,
      }),
    ).toMatchObject({ current: 7, limit: 8 });
  });

  it("does not alias a legacy name for a quota type it does not belong to", () => {
    // `cap` is the workspace cap's legacy limit, not the context cap's.
    expect(
      normalizeGate(429, "QUOTA-001", { quota_type: "contexts", cap: 9 }),
    ).toEqual({ state: "quota", quotaType: "contexts" });
  });

  it("ignores a non-numeric current/limit", () => {
    expect(
      normalizeGate(429, "QUOTA-001", {
        gate: "quota",
        quota_type: "contexts",
        current: "1",
        limit: null,
      }),
    ).toEqual({ state: "quota", quotaType: "contexts" });
    expect(
      normalizeGate(429, "QUOTA-001", {
        quota_type: "workspace_limit_reached",
        owned_count: "2",
        cap: Number.NaN,
      }),
    ).toEqual({ state: "quota", quotaType: "workspace_limit_reached" });
  });
});

describe("normalizeGate — fields", () => {
  it("does not narrow required_plan to the four OSS tiers", () => {
    // An operator-defined tier must survive: dropping it would turn a valid
    // upgrade path into "no tier has this feature".
    expect(
      normalizeGate(403, "FEAT-001", {
        gate: "plan",
        feature: "connectors",
        required_plan: "enterprise_plus",
        required_plan_display: "Enterprise+",
        current_plan: "team_custom",
      }),
    ).toMatchObject({
      requiredPlan: "enterprise_plus",
      requiredPlanLabel: "Enterprise+",
      currentPlan: "team_custom",
    });
  });

  it("treats null tier fields as absent", () => {
    const facts = normalizeGate(403, "FEAT-001", {
      gate: "plan",
      feature: "secret_store",
      required_plan: null,
      required_plan_display: null,
      current_plan: null,
    });
    expect(facts).toEqual({ state: "plan", feature: "secret_store" });
  });

  it("carries no counts on QUOTA-002 (USD floats under other names)", () => {
    expect(
      normalizeGate(429, "QUOTA-002", {
        gate: "quota",
        quota_type: "embedding_spend_daily",
        period: "daily",
        cap_usd: 5.0,
        current_usd: 5.25,
      }),
    ).toEqual({ state: "quota", quotaType: "embedding_spend_daily" });
  });

  it("carries no counts on the rate-limit 429", () => {
    expect(
      normalizeGate(429, "QUOTA-001", {
        gate: "quota",
        quota_type: "api_rest_daily",
        retry_after: 86400,
      }),
    ).toEqual({ state: "quota", quotaType: "api_rest_daily" });
  });

  it("keeps counts and quotaType off a non-quota gate", () => {
    expect(
      normalizeGate(403, "FEAT-001", {
        gate: "plan",
        feature: "sleep_mode",
        quota_type: "sleep_enabled_contexts",
        current: 0,
        limit: 0,
        resets_at: "2026-09-24T00:00:00Z",
      }),
    ).toEqual({ state: "plan", feature: "sleep_mode" });
  });
});

describe("GATE_KEYS / QUOTA_TYPE_TO_GATE_KEY", () => {
  it("lists every gate key exactly once", () => {
    expect(GATE_KEYS).toHaveLength(23);
    expect(new Set(GATE_KEYS).size).toBe(GATE_KEYS.length);
  });

  it("has exactly the server's frozen QUOTA_TYPES as its domain", () => {
    expect(Object.keys(QUOTA_TYPE_TO_GATE_KEY).sort()).toEqual(
      [...SERVER_QUOTA_TYPES].sort(),
    );
  });

  it("maps only onto GATE_KEYS", () => {
    for (const key of Object.values(QUOTA_TYPE_TO_GATE_KEY)) {
      expect(isGateKey(key)).toBe(true);
    }
  });

  it("isGateKey rejects prototype keys and the server-only feature names", () => {
    expect(isGateKey("constructor")).toBe(false);
    expect(isGateKey("sleep_mode")).toBe(false);
    expect(isGateKey(undefined)).toBe(false);
  });
});

describe("narrowCanUpgrade", () => {
  it.each<[FeatureGateState, string | undefined, boolean, boolean]>([
    ["plan", "pro", true, true],
    ["plan", undefined, true, true],
    ["plan", "pro", false, false],
    ["quota", "basic", true, true],
    ["quota", undefined, true, false],
    ["quota", "basic", false, false],
    ["deployment", undefined, true, false],
    ["allowlist", undefined, true, false],
    ["allowlist", "pro", true, false],
    ["role", undefined, true, false],
    ["pending", undefined, true, false],
    ["allowed", undefined, true, false],
  ])("%s with requiredPlan %s and raw %s → %s", (state, plan, raw, out) => {
    expect(narrowCanUpgrade(state, plan, raw)).toBe(out);
  });
});

describe("isBlocked", () => {
  it.each<[FeatureGateState, boolean]>([
    ["pending", false],
    ["allowed", false],
    ["plan", true],
    ["quota", true],
    ["deployment", true],
    ["role", true],
    ["allowlist", true],
  ])("%s → %s", (state, out) => {
    expect(isBlocked({ state })).toBe(out);
  });
});

describe("gateFromFacts", () => {
  const ENV_KEYS = [
    "NEXT_PUBLIC_PLAN_DISPLAY_NAMES",
    "NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME",
  ] as const;
  const savedEnv = ENV_KEYS.map((k) => [k, process.env[k]] as const);
  afterEach(() => {
    for (const [k, v] of savedEnv) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  const ctx = {
    fallbackKey: "contexts" as const,
    canUpgrade: true,
    locale: "en",
  };

  it("returns null when there are no facts", () => {
    expect(gateFromFacts(undefined, ctx)).toBeNull();
  });

  describe("feature resolution", () => {
    it("uses facts.feature when it is a GateKey", () => {
      expect(
        gateFromFacts({ state: "plan", feature: "team_invitations" }, ctx)
          ?.feature,
      ).toBe("team_invitations");
    });

    it("falls to the quota-type map when the wire names no GateKey", () => {
      expect(
        gateFromFacts(
          { state: "quota", quotaType: "workspace_limit_reached" },
          ctx,
        )?.feature,
      ).toBe("workspaces");
      // `sleep_mode` is the server's feature name; the client key is sleep_reports.
      expect(
        gateFromFacts(
          {
            state: "quota",
            feature: "sleep_mode",
            quotaType: "sleep_enabled_contexts",
          },
          ctx,
        )?.feature,
      ).toBe("sleep_reports");
    });

    it("falls back to the caller's key last", () => {
      expect(
        gateFromFacts(
          { state: "role" },
          { ...ctx, fallbackKey: "team_invitations" },
        )?.feature,
      ).toBe("team_invitations");
      expect(
        gateFromFacts(
          { state: "quota", quotaType: "constructor" },
          { ...ctx, fallbackKey: "members" },
        )?.feature,
      ).toBe("members");
    });
  });

  describe("labels", () => {
    it("resolves a canonical tier through the env label, not the server's", () => {
      delete process.env.NEXT_PUBLIC_PLAN_DISPLAY_NAMES;
      process.env.NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME = "Team";
      const gate = gateFromFacts(
        {
          state: "plan",
          feature: "team_invitations",
          requiredPlan: "pro",
          requiredPlanLabel: "L",
          currentPlan: "free",
        },
        ctx,
      );
      expect(gate?.planLabel).toBe("Team");
      expect(gate?.currentPlanLabel).toBe("S");
    });

    it("falls back to the server label for an operator-defined tier", () => {
      const gate = gateFromFacts(
        {
          state: "plan",
          requiredPlan: "enterprise_plus",
          requiredPlanLabel: "Enterprise+",
        },
        ctx,
      );
      expect(gate?.requiredPlan).toBe("enterprise_plus");
      expect(gate?.planLabel).toBe("Enterprise+");
    });

    it("falls back to the raw key when the server sent no label", () => {
      expect(
        gateFromFacts({ state: "plan", requiredPlan: "enterprise_plus" }, ctx)
          ?.planLabel,
      ).toBe("enterprise_plus");
      expect(
        gateFromFacts({ state: "quota", currentPlan: "team_custom" }, ctx)
          ?.currentPlanLabel,
      ).toBe("team_custom");
    });
  });

  describe("field presence by state", () => {
    const everything: Omit<FeatureGateFacts, "state"> = {
      feature: "memory_analysis",
      quotaType: "memory_analysis",
      requiredPlan: "pro",
      requiredPlanLabel: "L",
      currentPlan: "basic",
      current: 3,
      limit: 3,
      resetsAt: "2026-09-24T00:00:00Z",
    };

    it("plan: tier fields, no counts, raw canUpgrade", () => {
      expect(gateFromFacts({ state: "plan", ...everything }, ctx)).toEqual({
        state: "plan",
        feature: "memory_analysis",
        requiredPlan: "pro",
        planLabel: "L",
        currentPlan: "basic",
        currentPlanLabel: "M",
        canUpgrade: true,
      });
    });

    it("quota: tier fields and counts", () => {
      expect(gateFromFacts({ state: "quota", ...everything }, ctx)).toEqual({
        state: "quota",
        feature: "memory_analysis",
        requiredPlan: "pro",
        planLabel: "L",
        currentPlan: "basic",
        currentPlanLabel: "M",
        current: 3,
        limit: 3,
        resetsAt: "2026-09-24T00:00:00Z",
        canUpgrade: true,
      });
    });

    it.each<RefusedGateState>(["deployment", "allowlist"])(
      "%s: nothing but the feature, and never a CTA",
      (state) => {
        expect(gateFromFacts({ state, ...everything }, ctx)).toEqual({
          state,
          feature: "memory_analysis",
          canUpgrade: false,
        });
      },
    );

    it("role: the feature and its spec role, and never a CTA", () => {
      expect(gateFromFacts({ state: "role", ...everything }, ctx)).toEqual({
        state: "role",
        feature: "memory_analysis",
        requiredRole: WorkspaceRole.Owner,
        canUpgrade: false,
      });
    });

    it("a wire role gate gets requiredRole from GATE_SPECS (#1645)", () => {
      // AUTH-101 details are stripped server-side, so the wire never names
      // the role; the key the caller resolved to does.
      expect(
        gateFromFacts(
          { state: "role" },
          { ...ctx, fallbackKey: "team_invitations" },
        )?.requiredRole,
      ).toBe(WorkspaceRole.Admin);
      expect(
        gateFromFacts({ state: "role", feature: "memory_analysis" }, ctx)
          ?.requiredRole,
      ).toBe(WorkspaceRole.Owner);
    });

    it("a key with no spec role leaves requiredRole absent", () => {
      const gate = gateFromFacts(
        { state: "role" },
        { ...ctx, fallbackKey: "members" },
      );
      expect(gate).not.toHaveProperty("requiredRole");
    });

    it("never sets requiredRole on a non-role gate", () => {
      expect(
        gateFromFacts(
          { state: "plan", feature: "team_invitations" },
          { ...ctx, fallbackKey: "team_invitations" },
        ),
      ).not.toHaveProperty("requiredRole");
    });
  });

  describe("canUpgrade", () => {
    it("is false for a quota gate no higher tier lifts", () => {
      expect(
        gateFromFacts(
          { state: "quota", quotaType: "agents", current: 5, limit: 5 },
          ctx,
        )?.canUpgrade,
      ).toBe(false);
    });

    it("is false for a plan gate when the raw answer is false", () => {
      expect(
        gateFromFacts(
          { state: "plan", requiredPlan: "pro" },
          { ...ctx, canUpgrade: false },
        )?.canUpgrade,
      ).toBe(false);
    });

    it("is true for a plan gate even when no tier has the feature", () => {
      // The copy then names no tier; whether the CTA renders is the
      // consumer's call on `planLabel`, but the rule itself is flag ∧ owner.
      expect(gateFromFacts({ state: "plan" }, ctx)?.canUpgrade).toBe(true);
    });
  });
});

// ── #1645: the pre-check half ───────────────────────────────────────────────

/** A served tier row: everything off unless named. */
function tierRow(
  name: string,
  over: Partial<PlanTierFeature> = {},
): PlanTierFeature {
  return {
    name,
    display_name: name.toUpperCase(),
    max_contexts: 1,
    max_members: 1,
    owned_workspaces: 1,
    memory_limit: 1,
    memories_per_day: 1,
    storage_limit_bytes: 1,
    mcp_calls_per_day: 1,
    rest_calls_per_day: 0,
    public_calls_per_day: 0,
    max_resource_tokens: 0,
    max_connectors: 0,
    analysis_runs_per_day: 0,
    sleep_enabled_contexts_limit: 0,
    reranking: false,
    managed_embeddings: false,
    managed_llm: false,
    secret_store: true,
    shared_contexts: false,
    team_invitations: false,
    resources: false,
    connectors: false,
    public_contexts: false,
    ...over,
  };
}

/** The OSS default matrix, in the server's upgrade order (free → promax). */
const DEFAULT_TIERS: readonly PlanTierFeature[] = [
  tierRow("free", { max_contexts: 1 }),
  tierRow("basic", {
    max_contexts: 3,
    reranking: true,
    managed_embeddings: true,
  }),
  tierRow("pro", {
    max_contexts: 20,
    max_members: 10,
    reranking: true,
    managed_embeddings: true,
    managed_llm: true,
    shared_contexts: true,
    team_invitations: true,
    analysis_runs_per_day: 3,
    sleep_enabled_contexts_limit: 3,
  }),
  tierRow("promax", {
    max_contexts: 1000,
    max_members: 50,
    reranking: true,
    managed_embeddings: true,
    managed_llm: true,
    shared_contexts: true,
    team_invitations: true,
    analysis_runs_per_day: 15,
    sleep_enabled_contexts_limit: 15,
    resources: true,
    connectors: true,
    public_contexts: true,
    max_resource_tokens: 150,
    max_connectors: 50,
  }),
];

type ResolveInput = Parameters<typeof resolveGate>[0];

/** A resolved owner on the free tier, Plan page on — override per case. */
function input(
  key: GateKey,
  over: Partial<Omit<ResolveInput, "key">> = {},
): ResolveInput {
  return {
    key,
    tiers: DEFAULT_TIERS,
    planName: "free",
    workspaceResolved: true,
    features: { plan_page: true },
    role: "owner",
    canUpgrade: true,
    locale: "en",
    ...over,
  };
}

describe("resolveGate — the pending truth table (#1645)", () => {
  // Rows 1-4: a plan gate with no flags (shared_contexts).
  it("row 1: an unresolved matrix keeps a plan gate pending", () => {
    expect(resolveGate(input("shared_contexts", { tiers: null })).state).toBe(
      "pending",
    );
  });

  it("row 2: an unresolved workspace keeps a plan gate pending", () => {
    expect(
      resolveGate(
        input("shared_contexts", { workspaceResolved: false, planName: null }),
      ).state,
    ).toBe("pending");
  });

  it("row 3: a resolved matrix whose row has the feature answers allowed", () => {
    expect(
      resolveGate(input("shared_contexts", { planName: "pro" })).state,
    ).toBe("allowed");
  });

  it("row 4: a resolved matrix whose row lacks it answers plan, naming the tier", () => {
    const gate = resolveGate(input("shared_contexts", { planName: "basic" }));
    expect(gate.state).toBe("plan");
    expect(gate.requiredPlan).toBe("pro");
  });

  // Rows 5-11: gates with flags (managed_llm is default-off, reranking on).
  it("row 5: an unresolved /system/info keeps a flag gate pending, never deployment", () => {
    expect(
      resolveGate(input("managed_llm", { features: null, planName: "pro" }))
        .state,
    ).toBe("pending");
    expect(resolveGate(input("plan_page", { features: null })).state).toBe(
      "pending",
    );
  });

  it("row 6: a failed-closed /system/info blocks a default-off flag gate", () => {
    expect(
      resolveGate(input("managed_llm", { features: {}, planName: "pro" }))
        .state,
    ).toBe("deployment");
  });

  it("row 7: a failed-closed /system/info leaves a default-on flag passing (#1580)", () => {
    expect(
      resolveGate(input("reranking", { features: {}, planName: "basic" }))
        .state,
    ).toBe("allowed");
    expect(
      resolveGate(input("reranking", { features: {}, planName: "free" })).state,
    ).toBe("plan");
    expect(
      resolveGate(input("reranking", { features: {}, tiers: null })).state,
    ).toBe("pending");
  });

  it("row 8: an explicit false blocks a flag gate of either polarity", () => {
    expect(
      resolveGate(
        input("reranking", { features: { reranking: false }, planName: "pro" }),
      ).state,
    ).toBe("deployment");
    expect(
      resolveGate(
        input("managed_llm", {
          features: { managed_llm: false },
          planName: "pro",
        }),
      ).state,
    ).toBe("deployment");
  });

  it("row 9: a passing flag still waits for the matrix", () => {
    expect(
      resolveGate(
        input("managed_llm", { features: { managed_llm: true }, tiers: null }),
      ).state,
    ).toBe("pending");
  });

  it("row 10: a passing flag and a failing matrix test answer plan", () => {
    const gate = resolveGate(
      input("managed_llm", { features: { managed_llm: true }, planName: "basic" }),
    );
    expect(gate.state).toBe("plan");
    expect(gate.requiredPlan).toBe("pro");
  });

  it("row 11: a passing flag and a passing matrix test answer allowed", () => {
    expect(
      resolveGate(
        input("managed_llm", { features: { managed_llm: true }, planName: "pro" }),
      ).state,
    ).toBe("allowed");
  });

  it("row 12: no workspace at all (null, not loading) stays pending — never plan", () => {
    // The caller feeds `currentWorkspace !== null`, which is false forever
    // for a user with no workspace; `!loading` would be true and fail the
    // matrix test closed on an undefined plan.
    for (const key of ["shared_contexts", "resources", "team_invitations"] as const) {
      const gate = resolveGate(
        input(key, { workspaceResolved: false, planName: undefined, role: undefined }),
      );
      expect(gate.state).toBe("pending");
      expect(gate.canUpgrade).toBe(false);
    }
  });

  it("a matrix transport failure never resolves to plan (A1)", () => {
    // `tiers === null` is both "still fetching" and "gave up after three
    // attempts", by design. Neither may upsell, whatever else is known.
    for (const key of GATE_KEYS) {
      if (!("matrix" in GATE_SPECS[key])) continue;
      const gate = resolveGate(
        input(key, { tiers: null, features: { reranking: true, managed_llm: true } }),
      );
      expect(gate.state).toBe("pending");
      expect(gate).not.toHaveProperty("requiredPlan");
      expect(gate.canUpgrade).toBe(false);
    }
  });
});

describe("resolveGate — precedence (#1645)", () => {
  it("a resolved flag-off outranks a pending matrix (A2)", () => {
    expect(
      resolveGate(input("managed_llm", { features: {}, tiers: null })).state,
    ).toBe("deployment");
    expect(
      resolveGate(
        input("managed_llm", { features: {}, workspaceResolved: false }),
      ).state,
    ).toBe("deployment");
  });

  it("while /system/info is in flight the gate is pending, not deployment", () => {
    expect(
      resolveGate(input("managed_llm", { features: null, tiers: null })).state,
    ).toBe("pending");
    expect(
      resolveGate(input("cost_dashboard", { features: null })).state,
    ).toBe("pending");
  });

  it("role outranks plan: a member on a low tier is told about the role", () => {
    const gate = resolveGate(
      input("team_invitations", { planName: "free", role: "member" }),
    );
    expect(gate).toEqual({
      state: "role",
      feature: "team_invitations",
      requiredRole: WorkspaceRole.Admin,
      canUpgrade: false,
    });
  });

  it("an admin passes an admin-minimum gate and meets the plan", () => {
    expect(
      resolveGate(input("team_invitations", { planName: "free", role: "admin" }))
        .state,
    ).toBe("plan");
  });

  it("quota only fires once the feature itself is allowed", () => {
    const quota = { current: 5, limit: 5 };
    expect(
      resolveGate(input("shared_contexts", { planName: "basic", quota })).state,
    ).toBe("plan");
    expect(
      resolveGate(input("shared_contexts", { planName: "pro", quota })).state,
    ).toBe("quota");
    expect(
      resolveGate(
        input("shared_contexts", { planName: "pro", quota: { current: 4, limit: 5 } }),
      ).state,
    ).toBe("allowed");
  });

  it("a flag + role gate (plan_page) does not wait on the tier matrix", () => {
    expect(resolveGate(input("plan_page", { tiers: null })).state).toBe(
      "allowed",
    );
    expect(
      resolveGate(input("plan_page", { tiers: null, role: "admin" })),
    ).toMatchObject({ state: "role", requiredRole: WorkspaceRole.Owner });
    expect(
      resolveGate(input("plan_page", { features: {}, role: "owner" })).state,
    ).toBe("deployment");
  });

  it("cost_dashboard needs both of its flags", () => {
    const both = { byok: true, cost_display: true };
    expect(resolveGate(input("cost_dashboard", { features: both })).state).toBe(
      "allowed",
    );
    expect(
      resolveGate(input("cost_dashboard", { features: { byok: true } })).state,
    ).toBe("deployment");
  });
});

describe("resolveGate — field presence (#1645)", () => {
  it("pending, allowed and deployment carry the feature and nothing else", () => {
    expect(resolveGate(input("resources", { tiers: null }))).toEqual({
      state: "pending",
      feature: "resources",
      canUpgrade: false,
    });
    expect(resolveGate(input("resources", { planName: "promax" }))).toEqual({
      state: "allowed",
      feature: "resources",
      canUpgrade: false,
    });
    expect(
      resolveGate(input("managed_llm", { features: {}, planName: "pro" })),
    ).toEqual({ state: "deployment", feature: "managed_llm", canUpgrade: false });
  });

  it("plan: required and current tier with labels, raw canUpgrade", () => {
    expect(resolveGate(input("resources", { planName: "pro" }))).toEqual({
      state: "plan",
      feature: "resources",
      requiredPlan: "promax",
      planLabel: "XL",
      currentPlan: "pro",
      currentPlanLabel: "L",
      canUpgrade: true,
    });
    expect(
      resolveGate(input("resources", { planName: "pro", canUpgrade: false }))
        .canUpgrade,
    ).toBe(false);
  });

  it("a positive-limit plan gate reads the numeric column", () => {
    const gate = resolveGate(
      input("sleep_reports", { planName: "basic", role: "owner" }),
    );
    expect(gate.state).toBe("plan");
    expect(gate.requiredPlan).toBe("pro");
  });

  it("a plan name the matrix does not know fails closed", () => {
    expect(
      resolveGate(input("shared_contexts", { planName: "enterprise" })).state,
    ).toBe("plan");
  });
});

describe("requiredTierFor / requiredPlan (#1645)", () => {
  it("picks the lowest tier in the SERVED order, not PLAN_TIER_ORDER", () => {
    // An operator lists tiers in an order the client's union does not know.
    const tiers = [
      tierRow("starter"),
      tierRow("promax", { connectors: true }),
      tierRow("pro", { connectors: true }),
    ];
    expect(requiredTierFor(tiers, (t) => t.connectors === true)?.name).toBe(
      "promax",
    );
    expect(resolveGate(input("connectors", { tiers, planName: "starter" })))
      .toMatchObject({ state: "plan", requiredPlan: "promax" });
  });

  it("an operator override that moves shared_contexts to basic flows through with no code change", () => {
    const tiers = DEFAULT_TIERS.map((t) =>
      t.name === "basic" ? { ...t, shared_contexts: true } : t,
    );
    expect(
      resolveGate(input("shared_contexts", { tiers, planName: "basic" })).state,
    ).toBe("allowed");
    expect(
      resolveGate(input("shared_contexts", { tiers, planName: "free" })),
    ).toMatchObject({ state: "plan", requiredPlan: "basic", planLabel: "M" });
  });

  it("no tier has the feature: state stays plan with no requiredPlan", () => {
    const tiers = DEFAULT_TIERS.map((t) => ({ ...t, resources: false }));
    const gate = resolveGate(input("resources", { tiers, planName: "pro" }));
    expect(gate.state).toBe("plan");
    expect(gate).not.toHaveProperty("requiredPlan");
    expect(gate).not.toHaveProperty("planLabel");
    expect(requiredTierFor(tiers, (t) => t.resources === true)).toBeNull();
  });

  it("a non-canonical tier is labelled by its display_name; a canonical one by the env label", () => {
    const tiers = [
      tierRow("free"),
      tierRow("enterprise", { public_contexts: true, display_name: "Enterprise" }),
    ];
    expect(
      resolveGate(input("public_contexts", { tiers, planName: "free" })),
    ).toMatchObject({
      requiredPlan: "enterprise",
      planLabel: "Enterprise",
      currentPlanLabel: "S",
    });
    expect(resolvedPlanLabel("free", tiers, "Server S", "en")).toBe("S");
    expect(resolvedPlanLabel("enterprise", tiers, "Server E", "en")).toBe(
      "Enterprise",
    );
    expect(resolvedPlanLabel("team", tiers, "Team (server)", "en")).toBe(
      "Team (server)",
    );
    expect(resolvedPlanLabel("team", null, undefined, "en")).toBe("team");
  });
});

describe("shared_contexts resolves identically on both screens (#1645)", () => {
  it("one re-mapped matrix gives the contexts dialogs and context settings one answer", () => {
    // Both screens feed resolveGate the same cached matrix and workspace; a
    // tier-name compare on one of them is exactly what this pins against.
    const tiers = DEFAULT_TIERS.map((t) =>
      t.name === "basic" ? { ...t, shared_contexts: true } : t,
    );
    for (const planName of ["free", "basic", "pro"]) {
      const dialog = resolveGate(input("shared_contexts", { tiers, planName }));
      const settings = resolveGate(
        input("shared_contexts", { tiers, planName }),
      );
      expect(settings).toEqual(dialog);
    }
    expect(
      resolveGate(input("shared_contexts", { tiers, planName: "basic" })).state,
    ).toBe("allowed");
  });
});

describe("enforcement mode (#1645 mechanism, P-7 decision open)", () => {
  it("a refuse-mode feature blocks", () => {
    expect(GATE_SPECS.reranking.enforcement).toBe("refuse");
    expect(
      resolveGate(input("reranking", { features: { reranking: true } })).state,
    ).toBe("plan");
  });

  it("a degrade-mode feature resolves allowed with degraded: true", () => {
    // Simulate the one-line flip P-7 leaves open.
    const spec = GATE_SPECS.reranking as { enforcement: "refuse" | "degrade" };
    spec.enforcement = "degrade";
    try {
      expect(
        resolveGate(input("reranking", { features: { reranking: true } })),
      ).toEqual({
        state: "allowed",
        feature: "reranking",
        canUpgrade: false,
        degraded: true,
      });
    } finally {
      spec.enforcement = "refuse";
    }
  });

  it("the served feature_enforcement map is not consulted (the divergence is recorded there)", () => {
    // #1648 serves `reranking: "degrades"` on every row; the client keeps
    // its local "refuse" until the product decision is made.
    const served: Record<string, FeatureEnforcementMode> = {
      reranking: "degrades",
    };
    const tiers = DEFAULT_TIERS.map((t) => ({
      ...t,
      feature_enforcement: served,
    }));
    expect(
      resolveGate(input("reranking", { tiers, features: { reranking: true } })),
    ).toMatchObject({ state: "plan", requiredPlan: "basic" });
  });
});

describe("quotaGate (#1645)", () => {
  const args = {
    key: "contexts" as const,
    planName: "free",
    tiers: DEFAULT_TIERS,
    canUpgrade: true,
    locale: "en",
  };

  it("an unknown cap (limit 0) never blocks", () => {
    expect(quotaGate({ ...args, current: 7, limit: 0 })).toEqual({
      state: "allowed",
      feature: "contexts",
      canUpgrade: false,
    });
  });

  it("current below limit is allowed", () => {
    expect(quotaGate({ ...args, current: 0, limit: 1 }).state).toBe("allowed");
  });

  it("current >= limit is a quota gate with both numbers and the tier that raises the cap", () => {
    expect(quotaGate({ ...args, current: 1, limit: 1 })).toEqual({
      state: "quota",
      feature: "contexts",
      requiredPlan: "basic",
      planLabel: "M",
      currentPlan: "free",
      currentPlanLabel: "S",
      current: 1,
      limit: 1,
      canUpgrade: true,
    });
  });

  it("no tier raises the cap: no requiredPlan, so no CTA", () => {
    const gate = quotaGate({ ...args, current: 1000, limit: 1000 });
    expect(gate.state).toBe("quota");
    expect(gate).not.toHaveProperty("requiredPlan");
    expect(gate.canUpgrade).toBe(false);
  });

  it("storage and agents never offer an upgrade (P-6)", () => {
    for (const key of ["storage", "agents"] as const) {
      const gate = quotaGate({ ...args, key, current: 5, limit: 5 });
      expect(gate.state).toBe("quota");
      expect(gate.canUpgrade).toBe(false);
    }
  });

  it("an unresolved matrix still reports the quota, without a tier or a CTA", () => {
    const gate = quotaGate({ ...args, tiers: null, current: 1, limit: 1 });
    expect(gate.state).toBe("quota");
    expect(gate).not.toHaveProperty("requiredPlan");
    expect(gate.canUpgrade).toBe(false);
  });
});

describe("gateFromFacts with the matrix (#1645)", () => {
  const ENV_KEYS = ["NEXT_PUBLIC_PLAN_DISPLAY_NAMES"] as const;
  const savedEnv = ENV_KEYS.map((k) => [k, process.env[k]] as const);
  afterEach(() => {
    for (const [k, v] of savedEnv) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  const ctx = {
    fallbackKey: "contexts" as const,
    canUpgrade: true,
    locale: "en",
    tiers: DEFAULT_TIERS,
  };

  it("a FEAT-001 refusal has the same shape as the pre-check for the same workspace", () => {
    const wire = gateFromFacts(
      {
        state: "plan",
        feature: "team_invitations",
        requiredPlan: "pro",
        requiredPlanLabel: "L",
        currentPlan: "basic",
      },
      { ...ctx, fallbackKey: "team_invitations" },
    );
    const precheck = resolveGate(
      input("team_invitations", { planName: "basic" }),
    );
    expect(wire).toEqual(precheck);
  });

  it("gateFromFacts and quotaGate agree on canUpgrade for the same contexts refusal", () => {
    const facts: FeatureGateFacts = {
      state: "quota",
      quotaType: "contexts",
      current: 1,
      limit: 1,
      requiredPlan: "basic",
      requiredPlanLabel: "M",
      currentPlan: "free",
    };
    for (const raw of [true, false]) {
      const wire = gateFromFacts(facts, { ...ctx, canUpgrade: raw });
      const local = quotaGate({
        key: "contexts",
        current: 1,
        limit: 1,
        planName: "free",
        tiers: DEFAULT_TIERS,
        canUpgrade: raw,
        locale: "en",
      });
      expect(wire?.canUpgrade).toBe(local.canUpgrade);
      expect(wire).toEqual(local);
    }
  });

  it("a QUOTA-001 refusal keeps its numbers", () => {
    expect(
      gateFromFacts(
        { state: "quota", quotaType: "members", current: 10, limit: 10 },
        ctx,
      ),
    ).toMatchObject({
      state: "quota",
      feature: "members",
      current: 10,
      limit: 10,
      requiredPlan: "promax",
    });
  });

  it("an older backend that names no tier falls back to the matrix scan", () => {
    // Pre-#1644 FEAT-001: no required_plan in details.
    expect(
      gateFromFacts({ state: "plan", feature: "resources" }, ctx),
    ).toMatchObject({ requiredPlan: "promax", planLabel: "XL", canUpgrade: true });
    // ... and the sleep refusal, under the server's own feature name.
    expect(
      gateFromFacts(
        { state: "plan", feature: "sleep_mode" },
        { ...ctx, fallbackKey: "sleep_reports" },
      ),
    ).toMatchObject({ feature: "sleep_reports", requiredPlan: "pro" });
  });

  it("an operator tier on the wire is labelled from the matrix before the server's label", () => {
    const tiers = [
      ...DEFAULT_TIERS,
      tierRow("enterprise", { display_name: "Enterprise (matrix)" }),
    ];
    expect(
      gateFromFacts(
        {
          state: "plan",
          requiredPlan: "enterprise",
          requiredPlanLabel: "Enterprise (server)",
        },
        { ...ctx, tiers },
      )?.planLabel,
    ).toBe("Enterprise (matrix)");
  });

  it("the matrix scan never promotes a deployment or allowlist refusal to an upsell", () => {
    for (const state of ["deployment", "allowlist"] as const) {
      const gate = gateFromFacts({ state, feature: "resources" }, ctx);
      expect(gate).toEqual({ state, feature: "resources", canUpgrade: false });
    }
  });

  it("undefined facts still return null", () => {
    expect(gateFromFacts(undefined, ctx)).toBeNull();
  });
});

describe("GATE_SPECS key space (#1645)", () => {
  /**
   * Copied from `backend/src/config/plan_tiers.py` `KNOWN_FEATURES`
   * (`FEATURE_MIN_PLANS` ∪ every code-default tier's features).
   */
  const SERVER_KNOWN_FEATURES = [
    "api_keys",
    "reranking",
    "oauth",
    "team_invitations",
    "shared_contexts",
    "public_contexts",
    "memory_analysis",
    "managed_embeddings",
    "managed_llm",
    "resources",
    "connectors",
    "secret_store",
  ];

  it("every plan GateKey is a server feature key", () => {
    const planKeys = GATE_KEYS.filter(
      (k) => "matrix" in GATE_SPECS[k] && k !== "sleep_reports",
    );
    expect(planKeys.length).toBe(10);
    for (const key of planKeys) {
      expect(SERVER_KNOWN_FEATURES).toContain(key);
    }
  });

  it("keeps the #1644 vocabulary exactly", () => {
    expect([...GATE_KEYS].sort()).toEqual(
      [
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
      ].sort(),
    );
  });

  it("no spec role is ever Member", () => {
    for (const key of GATE_KEYS) {
      const spec = GATE_SPECS[key] as { role?: WorkspaceRole };
      if (spec.role) {
        expect([WorkspaceRole.Owner, WorkspaceRole.Admin]).toContain(spec.role);
      }
    }
  });

  it("flag polarity is per flag: only the reranker is default-on", () => {
    const defaultOn = GATE_KEYS.flatMap((k) => {
      const spec = GATE_SPECS[k] as {
        flags?: readonly { key: string; whenAbsent: boolean }[];
      };
      return (spec.flags ?? []).filter((f) => f.whenAbsent).map((f) => f.key);
    });
    expect(defaultOn).toEqual(["reranking"]);
    // A failed /system/info (`{}`) keeps the reranker, hides the rest.
    const failed: SystemFeatures = {};
    expect(
      resolveGate(input("reranking", { features: failed, planName: "pro" }))
        .state,
    ).toBe("allowed");
    expect(resolveGate(input("byok", { features: failed })).state).toBe(
      "deployment",
    );
  });
});
