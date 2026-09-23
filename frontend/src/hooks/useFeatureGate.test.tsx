/**
 * Tests for useFeatureGate / useFeatureGates (#1645).
 *
 * The rules live in `resolveGate` and are pinned in featureGates.test.ts;
 * this suite pins the binding: the real matrix and /system/info caches (the
 * two API calls are the only mocks), one fetch each however many gates, the
 * canUpgrade inputs, and the pending windows end to end. Each test gets a
 * fresh module graph so the module-level caches do not leak.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PlanTierFeature } from "@/lib/api/workspaces";
import type { FeatureGate } from "@/lib/gates/featureGates";

type Workspace = {
  plan_name: string;
  current_user_role?: string | null;
} | null;

let workspace: { currentWorkspace: Workspace; loading: boolean };

function row(name: string, over: Partial<PlanTierFeature>): PlanTierFeature {
  return {
    name,
    display_name: name,
    shared_contexts: false,
    public_contexts: false,
    team_invitations: false,
    resources: false,
    connectors: false,
    sleep_enabled_contexts_limit: 0,
    ...over,
  } as PlanTierFeature;
}

const TIERS = [
  row("free", {}),
  row("basic", {}),
  row("pro", {
    shared_contexts: true,
    team_invitations: true,
    sleep_enabled_contexts_limit: 3,
  }),
  row("promax", {
    shared_contexts: true,
    team_invitations: true,
    public_contexts: true,
    resources: true,
    connectors: true,
    sleep_enabled_contexts_limit: 15,
  }),
];

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  workspace = {
    currentWorkspace: { plan_name: "free", current_user_role: "owner" },
    loading: false,
  };
});

async function load(
  getPlanTierMatrix: () => Promise<unknown>,
  getSystemInfo: () => Promise<unknown>,
) {
  vi.doMock("@/lib/api/workspaces", () => ({ getPlanTierMatrix }));
  vi.doMock("@/lib/api/system", () => ({ getSystemInfo }));
  vi.doMock("@/contexts/WorkspaceContext", () => ({
    useWorkspace: () => workspace,
  }));
  vi.doMock("next-intl", () => ({ useLocale: () => "en" }));
  return import("./useFeatureGate");
}

const info = (features: Record<string, boolean>) => ({
  name: "",
  version: "",
  description: "",
  environment: "",
  features,
});

const out = (id = "out") => screen.getByTestId(id).textContent;
const summary = (g: FeatureGate) =>
  `${g.state}${g.planLabel ? `@${g.planLabel}` : ""}${g.canUpgrade ? "+cta" : ""}`;

describe("useFeatureGate (#1645)", () => {
  it("is pending on first render and resolves from the shared caches", async () => {
    const { useFeatureGate } = await load(
      vi.fn().mockResolvedValue(TIERS),
      vi.fn().mockResolvedValue(info({ plan_page: true })),
    );
    function Harness() {
      return (
        <div data-testid="out">
          {summary(useFeatureGate("shared_contexts"))}
        </div>
      );
    }

    render(<Harness />);
    // No upsell-able answer before the matrix lands.
    expect(out()).toBe("pending");
    await waitFor(() => expect(out()).toBe("plan@L+cta"));
  });

  it("N gates issue exactly one /plans/tiers and one /system/info", async () => {
    const getPlanTierMatrix = vi.fn().mockResolvedValue(TIERS);
    const getSystemInfo = vi.fn().mockResolvedValue(info({ plan_page: true }));
    const { useFeatureGate, useFeatureGates } = await load(
      getPlanTierMatrix,
      getSystemInfo,
    );
    function Many() {
      const gates = useFeatureGates([
        "public_contexts",
        "shared_contexts",
        "sleep_reports",
      ]);
      return <div data-testid="many">{gates.sleep_reports.state}</div>;
    }
    function One() {
      return <div data-testid="one">{useFeatureGate("resources").state}</div>;
    }

    render(
      <>
        <Many />
        <One />
        <One />
      </>,
    );
    await waitFor(() => expect(out("many")).toBe("plan"));
    expect(getPlanTierMatrix).toHaveBeenCalledTimes(1);
    expect(getSystemInfo).toHaveBeenCalledTimes(1);
  });

  it("canUpgrade is false while /system/info is pending, true for an owner once plan_page is on", async () => {
    const infoCall = deferred<unknown>();
    const { useFeatureGate } = await load(
      vi.fn().mockResolvedValue(TIERS),
      () => infoCall.promise,
    );
    function Harness() {
      return (
        <div data-testid="out">{summary(useFeatureGate("resources"))}</div>
      );
    }

    render(<Harness />);
    // The plan answer is known before /system/info is — and carries no CTA.
    await waitFor(() => expect(out()).toBe("plan@XL"));
    await act(async () => {
      infoCall.resolve(info({ plan_page: true }));
    });
    await waitFor(() => expect(out()).toBe("plan@XL+cta"));
  });

  it("canUpgrade is false for an admin on a plan_page-enabled deployment", async () => {
    workspace = {
      currentWorkspace: { plan_name: "free", current_user_role: "admin" },
      loading: false,
    };
    const infoCall = deferred<unknown>();
    const { useFeatureGate, useFeatureGates } = await load(
      vi.fn().mockResolvedValue(TIERS),
      () => infoCall.promise,
    );
    function Harness() {
      const { plan_page } = useFeatureGates(["plan_page"]);
      return (
        <div data-testid="out">
          {`${summary(useFeatureGate("resources"))}|${plan_page.state}`}
        </div>
      );
    }

    render(<Harness />);
    await waitFor(() => expect(out()).toBe("plan@XL|pending"));
    await act(async () => {
      infoCall.resolve(info({ plan_page: true }));
    });
    // /system/info has landed (the Plan page gate answers for this admin),
    // and still no CTA: the Plan page's data call is owner-only.
    await waitFor(() => expect(out()).toBe("plan@XL|role"));
  });

  it("a user with no workspace (null, not loading) stays pending — never plan", async () => {
    workspace = { currentWorkspace: null, loading: false };
    const getPlanTierMatrix = vi.fn().mockResolvedValue(TIERS);
    const { useFeatureGate } = await load(
      getPlanTierMatrix,
      vi.fn().mockResolvedValue(info({ plan_page: true })),
    );
    function Harness() {
      return (
        <div data-testid="out">
          {summary(useFeatureGate("shared_contexts"))}
        </div>
      );
    }

    render(<Harness />);
    await waitFor(() => expect(getPlanTierMatrix).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    expect(out()).toBe("pending");
  });

  it("useFeatureGate(key) and useFeatureGates([key])[key] agree — shared_contexts on both screens", async () => {
    // An operator re-maps shared_contexts onto basic: both call shapes (the
    // contexts dialogs' single gate and context settings' set) must follow.
    workspace = {
      currentWorkspace: { plan_name: "basic", current_user_role: "owner" },
      loading: false,
    };
    const remapped = TIERS.map((t) =>
      t.name === "basic" ? { ...t, shared_contexts: true } : t,
    );
    const { useFeatureGate, useFeatureGates } = await load(
      vi.fn().mockResolvedValue(remapped),
      vi.fn().mockResolvedValue(info({})),
    );
    const seen: FeatureGate[][] = [];
    function Harness() {
      const one = useFeatureGate("shared_contexts");
      const set = useFeatureGates([
        "public_contexts",
        "shared_contexts",
        "sleep_reports",
      ]);
      seen.push([one, set.shared_contexts]);
      return <div data-testid="out">{one.state}</div>;
    }

    render(<Harness />);
    await waitFor(() => expect(out()).toBe("allowed"));
    for (const [one, fromSet] of seen) expect(fromSet).toEqual(one);
  });

  it("keeps the same descriptor across re-renders while its inputs are unchanged", async () => {
    const { useFeatureGates } = await load(
      vi.fn().mockResolvedValue(TIERS),
      vi.fn().mockResolvedValue(info({ plan_page: true })),
    );
    const seen: FeatureGate[] = [];
    function Harness({ tick }: { tick: number }) {
      // A fresh key array every render, as call sites write it.
      const gates = useFeatureGates(["resources", "connectors"]);
      seen.push(gates.resources);
      return <div data-testid="out">{`${gates.resources.state}:${tick}`}</div>;
    }

    const { rerender } = render(<Harness tick={0} />);
    await waitFor(() => expect(out()).toBe("plan:0"));
    const settled = seen[seen.length - 1];
    rerender(<Harness tick={1} />);
    expect(out()).toBe("plan:1");
    expect(seen[seen.length - 1]).toBe(settled);
  });
});
