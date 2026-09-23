/**
 * Tests for useErrorGate (#1644).
 *
 * The hook's job is small — `instanceof ApiError`, then feed `gateFromFacts`
 * the RAW upgrade answer — but it is where a server refusal meets the CTA
 * rule, so the rule's invariants are pinned here end to end: an allowlist or
 * deployment gate can never offer an upgrade, even to an owner on a
 * deployment with the Plan page, and neither can a quota gate that names no
 * higher tier.
 */

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/base";
import type { PlanTierFeature } from "@/lib/api/workspaces";
import { WorkspaceRole } from "@/lib/auth/rbac";
import type {
  FeatureGateFacts,
  RefusedGateState,
} from "@/lib/gates/featureGates";

import { useErrorGate } from "./useErrorGate";

let mockFeatures: Record<string, boolean> | null = { plan_page: true };
let mockWorkspace: {
  currentWorkspace: { current_user_role?: string | null } | null;
  loading: boolean;
} = { currentWorkspace: { current_user_role: "owner" }, loading: false };

vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockWorkspace,
}));

vi.mock("next-intl", () => ({
  useLocale: () => "en",
}));

// #1645: the shared tier matrix is passed through. `null` (unresolved) by
// default, so every case above the matrix block behaves exactly as in #1644.
let mockTiers: PlanTierFeature[] | null = null;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => mockTiers,
}));

function refusal(gate: FeatureGateFacts | undefined, status = 403): ApiError {
  return new ApiError({ message: "refused", status, gate });
}

function run(err: unknown, fallbackKey = "contexts" as const) {
  return renderHook(() => useErrorGate(err, fallbackKey)).result.current;
}

beforeEach(() => {
  // The CTA-friendliest context there is: Plan page on, member is the owner.
  mockFeatures = { plan_page: true };
  mockWorkspace = {
    currentWorkspace: { current_user_role: "owner" },
    loading: false,
  };
  mockTiers = null;
});

describe("useErrorGate — not a gate refusal", () => {
  it.each([
    ["a plain Error", new Error("boom")],
    ["a string", "boom"],
    ["null", null],
    ["undefined", undefined],
  ])("returns null for %s", (_label, err) => {
    expect(run(err)).toBeNull();
  });

  it("returns null for a bare 403 ApiError with no gate", () => {
    expect(run(refusal(undefined, 403))).toBeNull();
  });

  it("returns null for a bare 429 ApiError with no gate", () => {
    expect(run(refusal(undefined, 429))).toBeNull();
  });
});

describe("useErrorGate — the CTA invariant", () => {
  it.each<RefusedGateState>(["allowlist", "deployment", "role"])(
    "canUpgrade is false for a %s gate even for an owner with the Plan page on",
    (state) => {
      // Even when the wire (wrongly) names a tier, no upgrade is offered.
      const gate = run(
        refusal({
          state,
          feature: "memory_analysis",
          requiredPlan: "pro",
          requiredPlanLabel: "L",
        }),
      );
      expect(gate?.state).toBe(state);
      expect(gate?.canUpgrade).toBe(false);
      // ...and allowlist / deployment copy stays plan-neutral.
      expect(gate).not.toHaveProperty("requiredPlan");
      expect(gate).not.toHaveProperty("planLabel");
    },
  );

  it("canUpgrade is false for a quota gate with no requiredPlan", () => {
    const gate = run(
      refusal(
        { state: "quota", quotaType: "agents", current: 5, limit: 5 },
        429,
      ),
    );
    expect(gate?.state).toBe("quota");
    expect(gate?.canUpgrade).toBe(false);
  });

  it("canUpgrade is true for a quota gate a higher tier lifts", () => {
    const gate = run(
      refusal(
        {
          state: "quota",
          quotaType: "contexts",
          current: 1,
          limit: 1,
          requiredPlan: "basic",
        },
        429,
      ),
    );
    expect(gate?.canUpgrade).toBe(true);
    expect(gate?.planLabel).toBe("M");
  });

  it("canUpgrade is true for a plan gate for an owner with the Plan page on", () => {
    const gate = run(
      refusal({ state: "plan", feature: "connectors", requiredPlan: "promax" }),
    );
    expect(gate).toMatchObject({
      state: "plan",
      feature: "connectors",
      requiredPlan: "promax",
      planLabel: "XL",
      canUpgrade: true,
    });
  });
});

describe("useErrorGate — the raw answer (canUpgradeFrom)", () => {
  const plan: FeatureGateFacts = {
    state: "plan",
    feature: "connectors",
    requiredPlan: "promax",
  };

  it("canUpgrade is false for a plan gate when the member is not an owner", () => {
    mockWorkspace = {
      currentWorkspace: { current_user_role: "admin" },
      loading: false,
    };
    expect(run(refusal(plan))?.canUpgrade).toBe(false);
  });

  it("canUpgrade is false for a plan gate when the deployment has no Plan page", () => {
    mockFeatures = {};
    expect(run(refusal(plan))?.canUpgrade).toBe(false);
  });

  it("canUpgrade is false while /system/info is unresolved — no CTA flash", () => {
    mockFeatures = null;
    const gate = run(refusal(plan));
    // The explanation survives; only the CTA is withheld.
    expect(gate?.state).toBe("plan");
    expect(gate?.canUpgrade).toBe(false);
  });

  it("canUpgrade is false while the workspace is hydrating", () => {
    mockWorkspace = { currentWorkspace: null, loading: true };
    expect(run(refusal(plan))?.canUpgrade).toBe(false);
  });
});

describe("useErrorGate — feature resolution", () => {
  it("falls back to the caller's key when the wire names none", () => {
    // AUTH-101: details are stripped server-side, so only the caller knows
    // what was being attempted.
    const gate = renderHook(() =>
      useErrorGate(refusal({ state: "role" }), "team_invitations"),
    ).result.current;
    // #1645: the wire never names the role; GATE_SPECS does.
    expect(gate).toEqual({
      state: "role",
      feature: "team_invitations",
      requiredRole: WorkspaceRole.Admin,
      canUpgrade: false,
    });
  });
});

describe("useErrorGate — the matrix is passed through (#1645)", () => {
  const row = (name: string, over: Partial<PlanTierFeature>) =>
    ({
      name,
      display_name: name,
      connectors: false,
      ...over,
    }) as PlanTierFeature;

  it("a refusal from a server that names no tier gets the matrix scan", () => {
    mockTiers = [
      row("free", {}),
      row("team", { connectors: true, display_name: "Team" }),
    ];
    const gate = run(refusal({ state: "plan", feature: "connectors" }));
    expect(gate).toMatchObject({
      state: "plan",
      requiredPlan: "team",
      planLabel: "Team",
      canUpgrade: true,
    });
  });

  it("without the matrix the same refusal names no tier", () => {
    const gate = run(refusal({ state: "plan", feature: "connectors" }));
    expect(gate).not.toHaveProperty("requiredPlan");
  });
});
