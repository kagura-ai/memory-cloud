/**
 * Tests for the wire half of the gate descriptor (#1644).
 *
 * `normalizeGate` is the one place a refusal body becomes gate facts, so its
 * table is pinned here case by case — above all "never infer a gate from a
 * bare 403/429" and the legacy count aliases an older server still sends.
 */

import { afterEach, describe, expect, it } from "vitest";

import {
  GATE_KEYS,
  QUOTA_TYPE_TO_GATE_KEY,
  gateFromFacts,
  isBlocked,
  isGateKey,
  narrowCanUpgrade,
  normalizeGate,
  type FeatureGateFacts,
  type FeatureGateState,
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

  it.each(["QUOTA-001", "QUOTA-002"])("maps %s to quota", (code) => {
    expect(normalizeGate(429, code, {})).toEqual({ state: "quota" });
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

    it.each<RefusedGateState>(["deployment", "role", "allowlist"])(
      "%s: nothing but the feature, and never a CTA",
      (state) => {
        expect(gateFromFacts({ state, ...everything }, ctx)).toEqual({
          state,
          feature: "memory_analysis",
          canUpgrade: false,
        });
      },
    );

    it("never sets requiredRole in #1644", () => {
      const gate = gateFromFacts(
        { state: "role" },
        {
          ...ctx,
          fallbackKey: "team_invitations",
        },
      );
      expect(gate).not.toHaveProperty("requiredRole");
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
