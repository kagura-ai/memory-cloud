/**
 * Tests for usePlanFeatures (#1560).
 *
 * The gated pages mock this hook away, so the matrix fetch, the by-name
 * lookup and the fail-closed paths are exercised here directly. Each test gets
 * a fresh module so the module-level cache doesn't leak across cases.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import type { PlanTierFeature } from "@/lib/api/workspaces";

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
});

// Only the fields the hook reads matter; the rest is filler for the type.
function tier(name: string, gates: Partial<PlanTierFeature>): PlanTierFeature {
  return {
    name,
    display_name: name,
    max_contexts: 1,
    max_members: 1,
    owned_workspaces: 1,
    memory_limit: 1,
    storage_limit_bytes: 1,
    mcp_calls_per_day: 1,
    rest_calls_per_day: 1,
    public_calls_per_day: 0,
    max_resource_tokens: 0,
    max_connectors: 0,
    analysis_runs_per_day: 0,
    sleep_enabled_contexts_limit: 0,
    reranking: false,
    managed_embeddings: false,
    secret_store: false,
    shared_contexts: false,
    team_invitations: false,
    resources: false,
    connectors: false,
    public_contexts: false,
    ...gates,
  };
}

// The matrix deliberately grants connectors to "pro" and nothing to "promax":
// a hook that still ranked tier NAMES would get both of these wrong.
const MATRIX = [
  tier("free", { secret_store: true }),
  tier("pro", {
    connectors: true,
    resources: true,
    shared_contexts: true,
    team_invitations: true,
    reranking: true,
  }),
  tier("promax", {}),
];

async function setup(
  getPlanTierMatrix: () => Promise<unknown>,
  workspace: { plan_name: string } | null,
) {
  vi.doMock("@/lib/api/workspaces", () => ({ getPlanTierMatrix }));
  vi.doMock("@/contexts/WorkspaceContext", () => ({
    useWorkspace: () => ({ currentWorkspace: workspace }),
  }));
  const { usePlanFeatures } = await import("./usePlanFeatures");
  function Harness() {
    const f = usePlanFeatures();
    return (
      <div data-testid="out">
        {f ? `loaded:${JSON.stringify(f)}` : "pending"}
      </div>
    );
  }
  return Harness;
}

const out = () => screen.getByTestId("out").textContent;

describe("planFeaturesFor (#1560)", () => {
  it("reads the API booleans for the named tier, not the tier's rank", async () => {
    const { planFeaturesFor } = await import("./usePlanFeatures");
    expect(planFeaturesFor(MATRIX, "pro")).toEqual({
      resources: true,
      connectors: true,
      public_contexts: false,
      shared_contexts: true, // #1583
      // #1645: every boolean column of the tier row, not just the four.
      team_invitations: true,
      reranking: true,
      managed_embeddings: false,
      managed_llm: false,
      secret_store: false,
    });
    expect(planFeaturesFor(MATRIX, "promax")).toEqual({
      resources: false,
      connectors: false,
      public_contexts: false,
      shared_contexts: false,
      team_invitations: false,
      reranking: false,
      managed_embeddings: false,
      managed_llm: false,
      secret_store: false,
    });
    expect(planFeaturesFor(MATRIX, "free").secret_store).toBe(true);
  });

  it("fails closed for an unknown plan and for booleans an older API omits", async () => {
    const { planFeaturesFor } = await import("./usePlanFeatures");
    const closed = {
      resources: false,
      connectors: false,
      public_contexts: false,
      shared_contexts: false,
      team_invitations: false,
      reranking: false,
      managed_embeddings: false,
      managed_llm: false,
      secret_store: false,
    };
    expect(planFeaturesFor(MATRIX, "enterprise")).toEqual(closed);
    expect(planFeaturesFor(MATRIX, undefined)).toEqual(closed);
    // Pre-#1551 payload: the three fields are simply absent.
    const legacy = { ...tier("pro", {}) } as Partial<PlanTierFeature>;
    delete legacy.resources;
    delete legacy.connectors;
    delete legacy.public_contexts;
    expect(planFeaturesFor([legacy as PlanTierFeature], "pro")).toEqual(closed);
    // `managed_llm` is optional on the wire (pre-#1569): absent reads false.
    const noLlm = { ...tier("pro", { managed_llm: true }) };
    delete noLlm.managed_llm;
    expect(planFeaturesFor([noLlm], "pro").managed_llm).toBe(false);
  });

  it("lists every boolean column of the tier row (#1645)", async () => {
    const { PLAN_FEATURE_KEYS } = await import("./usePlanFeatures");
    const booleanColumns = Object.entries(tier("x", { managed_llm: false }))
      .filter(([, v]) => typeof v === "boolean")
      .map(([k]) => k)
      .sort();
    expect([...PLAN_FEATURE_KEYS].sort()).toEqual(booleanColumns);
  });
});

describe("usePlanFeatures (#1560)", () => {
  it("is pending until the matrix resolves, then answers from the API booleans", async () => {
    const getPlanTierMatrix = vi.fn().mockResolvedValue(MATRIX);
    const Harness = await setup(getPlanTierMatrix, { plan_name: "pro" });

    render(<Harness />);
    // No upsell-able answer before the fetch lands.
    expect(out()).toBe("pending");
    expect(getPlanTierMatrix).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(out()).toContain("loaded:"));
    expect(out()).toContain('"connectors":true');
    expect(out()).toContain('"resources":true');
    expect(out()).toContain('"public_contexts":false');
  });

  it("usePlanFeature narrows to one gate and is null while pending", async () => {
    const getPlanTierMatrix = vi.fn().mockResolvedValue(MATRIX);
    vi.doMock("@/lib/api/workspaces", () => ({ getPlanTierMatrix }));
    vi.doMock("@/contexts/WorkspaceContext", () => ({
      useWorkspace: () => ({ currentWorkspace: { plan_name: "pro" } }),
    }));
    const { usePlanFeature } = await import("./usePlanFeatures");
    function Harness() {
      const connectors = usePlanFeature("connectors");
      const publicContexts = usePlanFeature("public_contexts");
      return (
        <div data-testid="out">
          {String(connectors)}:{String(publicContexts)}
        </div>
      );
    }

    render(<Harness />);
    expect(out()).toBe("null:null");
    await waitFor(() => expect(out()).toBe("true:false"));
  });

  it("stays pending while the workspace is still unresolved", async () => {
    const getPlanTierMatrix = vi.fn().mockResolvedValue(MATRIX);
    const Harness = await setup(getPlanTierMatrix, null);

    render(<Harness />);
    await waitFor(() => expect(getPlanTierMatrix).toHaveBeenCalled());
    // Matrix may be cached, but with no workspace there is nothing to gate.
    await Promise.resolve();
    expect(out()).toBe("pending");
  });

  it("stays pending (never `false`) when the fetch keeps failing, and a later mount retries", async () => {
    vi.useFakeTimers();
    try {
      const getPlanTierMatrix = vi.fn().mockRejectedValue(new Error("down"));
      const Harness = await setup(getPlanTierMatrix, { plan_name: "pro" });

      const { unmount } = render(<Harness />);
      // Three attempts with 500ms / 1000ms back-off before giving up.
      await vi.advanceTimersByTimeAsync(2000);
      expect(getPlanTierMatrix).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(0);
      // "Matrix unavailable" is not "not included": resolving to `false`
      // here would upsell an entitled tenant and let the connectors page
      // strip its one-time Slack install handle. Consumers stay pending.
      expect(out()).toBe("pending");

      // The failure is not cached — the next mount starts a fresh fetch and
      // answers normally once the API is back.
      unmount();
      vi.useRealTimers();
      getPlanTierMatrix.mockResolvedValue(MATRIX);
      render(<Harness />);
      expect(getPlanTierMatrix).toHaveBeenCalledTimes(4);
      await waitFor(() => expect(out()).toContain('"connectors":true'));
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("usePlanTierMatrixState (#1645)", () => {
  async function setupState(getPlanTierMatrix: () => Promise<unknown>) {
    vi.doMock("@/lib/api/workspaces", () => ({ getPlanTierMatrix }));
    vi.doMock("@/contexts/WorkspaceContext", () => ({
      useWorkspace: () => ({ currentWorkspace: { plan_name: "pro" } }),
    }));
    const mod = await import("./usePlanFeatures");
    function Harness() {
      const { tiers, failed } = mod.usePlanTierMatrixState();
      const legacy = mod.usePlanTierMatrix();
      return (
        <div data-testid="out">
          {`${tiers ? tiers.length : "null"}:${failed}:${legacy ? legacy.length : "null"}`}
        </div>
      );
    }
    return Harness;
  }

  it("shares one fetch with usePlanTierMatrix and reports the served rows", async () => {
    const getPlanTierMatrix = vi.fn().mockResolvedValue(MATRIX);
    const Harness = await setupState(getPlanTierMatrix);

    render(<Harness />);
    expect(out()).toBe("null:false:null");
    await waitFor(() => expect(out()).toBe("3:false:3"));
    expect(getPlanTierMatrix).toHaveBeenCalledTimes(1);
  });

  it("reports failed once the retried fetch gives up, while usePlanTierMatrix stays pending", async () => {
    vi.useFakeTimers();
    try {
      const getPlanTierMatrix = vi.fn().mockRejectedValue(new Error("down"));
      const Harness = await setupState(getPlanTierMatrix);

      render(<Harness />);
      await vi.advanceTimersByTimeAsync(2000);
      expect(getPlanTierMatrix).toHaveBeenCalledTimes(3);
      await vi.advanceTimersByTimeAsync(0);
      // The error channel is the state hook's alone: the plain matrix reader
      // keeps answering `null` (pending), never a terminal value.
      expect(out()).toBe("null:true:null");
    } finally {
      vi.useRealTimers();
    }
  });
});
