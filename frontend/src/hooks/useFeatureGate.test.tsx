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
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

// ── The whole truth table, end to end ───────────────────────────────────────
//
// Every cell of {tier matrix} x {/system/info} x {workspace} x {role, tier},
// through the REAL caches: a pending input is a promise that never settles, a
// failed one is an API call that rejects on all three attempts. The expected
// column is written from decisions.md §5.3 (precedence + pending truth table)
// and §1.6 (canUpgrade), not from resolveGate, so the two are checked against
// each other. The invariants below it restate the load-bearing rules on their
// own, so a wrong table entry cannot hide a violation.

type MatrixState = "pending" | "resolved" | "failed";
type InfoState = "pending" | "on" | "off" | "absent" | "failed";
type Role = "member" | "admin" | "owner";
type WorkspaceCell =
  | { kind: "unresolved" } // null, still loading
  | { kind: "none" } // null, not loading: no workspace at all (row 12)
  | { kind: "resolved"; role: Role; plan: "free" | "promax"; loading: boolean };

const MATRIX_STATES: readonly MatrixState[] = ["pending", "resolved", "failed"];
const INFO_STATES: readonly InfoState[] = [
  "pending",
  "on",
  "off",
  "absent",
  "failed",
];
const WORKSPACE_CELLS: readonly WorkspaceCell[] = [
  { kind: "unresolved" },
  { kind: "none" },
  ...(["member", "admin", "owner"] as const).flatMap((role) =>
    (["free", "promax"] as const).map((plan): WorkspaceCell => ({
      kind: "resolved",
      role,
      plan,
      loading: false,
    })),
  ),
  // A workspace switch in flight: the old workspace is still current.
  { kind: "resolved", role: "owner", plan: "free", loading: true },
];

/** Every flag a gate key below reads; "absent" is an older backend. */
const INFO_FEATURES: Record<
  Exclude<InfoState, "pending" | "failed">,
  Record<string, boolean>
> = {
  on: { plan_page: true, reranking: true, managed_llm: true },
  off: { plan_page: false, reranking: false, managed_llm: false },
  absent: {},
};

// Free lacks everything; promax has everything.
const TABLE_TIERS: PlanTierFeature[] = [
  row("free", {
    reranking: false,
    managed_llm: false,
    analysis_runs_per_day: 0,
    sleep_enabled_contexts_limit: 0,
  }),
  row("promax", {
    shared_contexts: true,
    team_invitations: true,
    reranking: true,
    managed_llm: true,
    analysis_runs_per_day: 15,
    sleep_enabled_contexts_limit: 15,
  }),
];

/**
 * The gate keys under test, described the way decisions.md §1.2 does:
 * which flags (and their polarity), which minimum role, which matrix test.
 */
/** The /system/info payload, under a name the cells' `info` does not shadow. */
const infoPayload = info;

const KEY_FACTS = {
  team_invitations: { flags: [], role: "admin", matrix: "team_invitations" },
  shared_contexts: { flags: [], role: null, matrix: "shared_contexts" },
  reranking: {
    flags: [{ key: "reranking", defaultOn: true }],
    role: null,
    matrix: "reranking",
  },
  sleep_reports: {
    flags: [],
    role: "admin",
    matrix: "sleep_enabled_contexts_limit",
  },
  memory_analysis: {
    flags: [],
    role: "owner",
    matrix: "analysis_runs_per_day",
  },
  managed_llm: {
    flags: [{ key: "managed_llm", defaultOn: false }],
    role: null,
    matrix: "managed_llm",
  },
  plan_page: {
    flags: [{ key: "plan_page", defaultOn: false }],
    role: "owner",
    matrix: null,
  },
} as const satisfies Record<
  string,
  {
    flags: readonly { key: string; defaultOn: boolean }[];
    role: "admin" | "owner" | null;
    matrix: keyof PlanTierFeature | null;
  }
>;
type TableKey = keyof typeof KEY_FACTS;
const TABLE_KEYS = Object.keys(KEY_FACTS) as TableKey[];

const ROLE_RANK: Record<Role, number> = { member: 1, admin: 2, owner: 3 };

/** decisions.md §5.3 + §1.6, cell by cell. Returns "state" or "plan+cta". */
function expectedCell(
  key: TableKey,
  matrix: MatrixState,
  info: InfoState,
  ws: WorkspaceCell,
): string {
  const facts = KEY_FACTS[key];
  const features =
    info === "pending" ? null : info === "failed" ? {} : INFO_FEATURES[info];

  // 1. deployment: only once /system/info has resolved (the failed {} too),
  //    each flag by its own polarity.
  if (
    features !== null &&
    facts.flags.some((f) =>
      f.defaultOn ? features[f.key] === false : features[f.key] !== true,
    )
  ) {
    return "deployment";
  }
  // 2. pending: an unresolved (or failed) matrix, an unresolved flag, or no
  //    resolved workspace — never an upsell.
  if (facts.flags.length > 0 && features === null) return "pending";
  if (facts.matrix !== null && matrix !== "resolved") return "pending";
  if (
    (facts.matrix !== null || facts.role !== null) &&
    ws.kind !== "resolved"
  ) {
    return "pending";
  }
  if (ws.kind !== "resolved") return "allowed"; // unreachable for these keys
  // 3. role, above plan: a member cannot buy their way to owner.
  if (facts.role !== null && ROLE_RANK[ws.role] < ROLE_RANK[facts.role]) {
    return "role";
  }
  // 4. plan. The CTA: the Plan page is on here (resolved true), the workspace
  //    has hydrated, and this member is the owner.
  if (facts.matrix !== null) {
    const tierRow = TABLE_TIERS.find((t) => t.name === ws.plan)!;
    const value = tierRow[facts.matrix];
    const passes = typeof value === "number" ? value > 0 : value === true;
    if (!passes) {
      const cta =
        features !== null &&
        features.plan_page === true &&
        !ws.loading &&
        ws.role === "owner";
      return cta ? "plan+cta" : "plan";
    }
  }
  return "allowed";
}

function cellName(ws: WorkspaceCell): string {
  return ws.kind === "resolved"
    ? `${ws.role}@${ws.plan}${ws.loading ? "(switching)" : ""}`
    : ws.kind;
}

describe("useFeatureGates — every cell of the truth table (#1645)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Render one harness per workspace cell under one (matrix, /system/info)
   * pair, all on the same real caches, and read back every key's answer.
   */
  async function observe(
    matrix: MatrixState,
    info: InfoState,
  ): Promise<{
    cells: Map<string, Record<TableKey, string>>;
    matrixCalls: number;
    infoCalls: number;
  }> {
    vi.resetModules();
    const never = () => new Promise<never>(() => {});
    const getPlanTierMatrix = vi.fn(
      matrix === "pending"
        ? never
        : matrix === "failed"
          ? () => Promise.reject(new Error("tiers down"))
          : () => Promise.resolve(TABLE_TIERS),
    );
    const getSystemInfo = vi.fn(
      info === "pending"
        ? never
        : info === "failed"
          ? () => Promise.reject(new Error("info down"))
          : () => Promise.resolve(infoPayload(INFO_FEATURES[info])),
    );
    const { createContext, useContext } = await import("react");
    const WsCtx = createContext<{
      currentWorkspace: Workspace;
      loading: boolean;
    }>({ currentWorkspace: null, loading: true });
    vi.doMock("@/lib/api/workspaces", () => ({ getPlanTierMatrix }));
    vi.doMock("@/lib/api/system", () => ({ getSystemInfo }));
    vi.doMock("@/contexts/WorkspaceContext", () => ({
      useWorkspace: () => useContext(WsCtx),
    }));
    vi.doMock("next-intl", () => ({ useLocale: () => "en" }));
    const { useFeatureGates } = await import("./useFeatureGate");

    function Cell({ id }: { id: string }) {
      const gates = useFeatureGates(TABLE_KEYS);
      return (
        <div data-testid={id}>
          {JSON.stringify(
            Object.fromEntries(
              TABLE_KEYS.map((key) => [
                key,
                `${gates[key].state}${gates[key].canUpgrade ? "+cta" : ""}`,
              ]),
            ),
          )}
        </div>
      );
    }

    vi.useFakeTimers();
    render(
      <>
        {WORKSPACE_CELLS.map((ws) => (
          <WsCtx.Provider
            key={cellName(ws)}
            value={
              ws.kind === "resolved"
                ? {
                    currentWorkspace: {
                      plan_name: ws.plan,
                      current_user_role: ws.role,
                    },
                    loading: ws.loading,
                  }
                : { currentWorkspace: null, loading: ws.kind === "unresolved" }
            }
          >
            <Cell id={cellName(ws)} />
          </WsCtx.Provider>
        ))}
      </>,
    );
    // Past both hooks' three attempts (500 ms + 1000 ms back-off).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    const cells = new Map<string, Record<TableKey, string>>();
    for (const ws of WORKSPACE_CELLS) {
      cells.set(
        cellName(ws),
        JSON.parse(screen.getByTestId(cellName(ws)).textContent ?? "{}"),
      );
    }
    return {
      cells,
      matrixCalls: getPlanTierMatrix.mock.calls.length,
      infoCalls: getSystemInfo.mock.calls.length,
    };
  }

  const PAIRS = MATRIX_STATES.flatMap((matrix) =>
    INFO_STATES.map((info) => [matrix, info] as const),
  );

  it.each(PAIRS)("matrix %s, /system/info %s", async (matrix, info) => {
    const { cells, matrixCalls, infoCalls } = await observe(matrix, info);

    // The failure cells really are failures: all three attempts spent.
    if (matrix === "failed") expect(matrixCalls).toBe(3);
    if (info === "failed") expect(infoCalls).toBe(3);

    const mismatches: string[] = [];
    for (const ws of WORKSPACE_CELLS) {
      const got = cells.get(cellName(ws))!;
      for (const key of TABLE_KEYS) {
        const want = expectedCell(key, matrix, info, ws);
        if (got[key] !== want) {
          mismatches.push(
            `${cellName(ws)} ${key}: got ${got[key]}, want ${want}`,
          );
        }

        const [state] = got[key].split("+");
        const hasCta = got[key].endsWith("+cta");
        const facts = KEY_FACTS[key];
        // No upsell while any input it depends on is pending or failed.
        if (state === "plan") {
          expect(matrix).toBe("resolved");
          expect(ws.kind).toBe("resolved");
        }
        // A matrix failure is pending (or a resolved flag-off), never plan.
        if (facts.matrix !== null && matrix !== "resolved") {
          expect(["pending", "deployment"]).toContain(state);
        }
        // An in-flight /system/info can never answer "deployment".
        if (info === "pending") expect(state).not.toBe("deployment");
        if (info === "pending" && facts.flags.length > 0) {
          expect(state).toBe("pending");
        }
        // A failed /system/info falls closed for every default-off flag.
        if (info === "failed" && facts.flags.some((f) => !f.defaultOn)) {
          expect(state).toBe("deployment");
        }
        // A CTA only on a plan gate, for the owner, with the Plan page known on.
        if (hasCta) {
          expect(state).toBe("plan");
          expect(info).toBe("on");
          expect(ws.kind === "resolved" && ws.role).toBe("owner");
          expect(ws.kind === "resolved" && ws.loading).toBe(false);
        }
        // No workspace at all: pending for every tier or role gate, never plan.
        if (
          ws.kind !== "resolved" &&
          (facts.matrix !== null || facts.role !== null)
        ) {
          expect(["pending", "deployment"]).toContain(state);
        }
      }
    }
    expect(mismatches).toEqual([]);
  });
});
