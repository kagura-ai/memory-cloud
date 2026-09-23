/**
 * Tests for the workspace Sleep Reports plan gate (#1137 / #1548).
 *
 * Sleep Maintenance is Pro-or-better: free/basic see the upgrade CTA routing
 * to the Plan page; pro AND promax render the report list. The gate must not
 * fire while WorkspaceContext is still loading.
 *
 * #1643: the CTA is withheld wherever the Plan page is unreachable — the
 * gate's title and description are the explanation and always render.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import WorkspaceSleepReportsPage from "./page";
import type { PlanTierFeature } from "@/lib/api/workspaces";

// ---------- Mocks ------------------------------------------------------------

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
}));

const stableT = (k: string) => k;
vi.mock("next-intl", () => ({
  useTranslations: () => stableT,
  useLocale: () => "en",
}));

let mockWorkspaceState: {
  currentWorkspace: { plan_name?: string; current_user_role?: string } | null;
  currentWorkspaceId: string | null;
  loading: boolean;
} = { currentWorkspace: null, currentWorkspaceId: null, loading: true };
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => mockWorkspaceState,
}));

// #1643: useCanUpgrade reads /system/info. Without this mock the real hook
// fires a jsdom fetch, retries three times and leaves a module-level cache
// that leaks between cases in this file. `null` = still resolving.
let mockFeatures: Record<string, boolean> | null = { plan_page: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

// #1645: the gate reads the shared tier matrix (`null` = still resolving).
// Default: the OSS matrix, so `plan_name` decides exactly as the tier's row
// does (Sleep Maintenance: `sleep_enabled_contexts_limit > 0`).
const OSS_TIERS = [
  { name: "free", display_name: "S", sleep_enabled_contexts_limit: 0 },
  { name: "basic", display_name: "M", sleep_enabled_contexts_limit: 0 },
  { name: "pro", display_name: "L", sleep_enabled_contexts_limit: 3 },
  { name: "promax", display_name: "XL", sleep_enabled_contexts_limit: 15 },
] as unknown as PlanTierFeature[];
let mockTiers: PlanTierFeature[] | null = OSS_TIERS;
vi.mock("@/hooks/usePlanFeatures", () => ({
  usePlanTierMatrix: () => mockTiers,
}));

vi.mock("@/lib/api", () => ({
  fetchWorkspaceSleepReports: vi.fn(),
}));

// The list self-fetches and is covered by its own tests; stub it to a marker
// so this suite stays focused on the gate.
vi.mock("@/components/sleep-reports/SleepReportsList", () => ({
  SleepReportsList: ({ ready }: { ready?: boolean }) => (
    <div data-testid="sleep-reports-list" data-ready={String(ready)} />
  ),
}));

// ---------- Helpers ----------------------------------------------------------

function setWorkspace(plan_name: string, current_user_role = "owner") {
  mockWorkspaceState = {
    currentWorkspace: { plan_name, current_user_role },
    currentWorkspaceId: "ws-1",
    loading: false,
  };
}

beforeEach(() => {
  mockPush.mockReset();
  mockFeatures = { plan_page: true };
  mockTiers = OSS_TIERS;
});

afterEach(() => {
  cleanup();
});

// ---------- Tests ------------------------------------------------------------

describe("WorkspaceSleepReportsPage plan gate", () => {
  it.each(["pro", "promax"] as const)(
    "%s renders the report list with no upgrade CTA",
    (plan) => {
      setWorkspace(plan);
      render(<WorkspaceSleepReportsPage />);
      expect(screen.getByTestId("sleep-reports-list")).toBeInTheDocument();
      expect(screen.queryByText("sleepReports.planGate.title")).toBeNull();
    },
  );

  it.each(["free", "basic"] as const)(
    "%s shows the upgrade CTA routing to the plan page",
    (plan) => {
      setWorkspace(plan);
      render(<WorkspaceSleepReportsPage />);
      expect(
        screen.getByText("sleepReports.planGate.title"),
      ).toBeInTheDocument();
      expect(screen.queryByTestId("sleep-reports-list")).toBeNull();
      fireEvent.click(
        screen.getByRole("button", { name: "sleepReports.planGate.action" }),
      );
      expect(mockPush).toHaveBeenCalledWith("/workspace/settings/plan");
    },
  );

  it("free: shows the plan-gate copy with no action when plan_page is off", () => {
    mockFeatures = {};
    setWorkspace("free");
    render(<WorkspaceSleepReportsPage />);

    expect(screen.getByText("sleepReports.planGate.title")).toBeInTheDocument();
    expect(
      screen.getByText("sleepReports.planGate.description"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "sleepReports.planGate.action" }),
    ).toBeNull();
  });

  it("free: shows the plan-gate copy with no action for an admin", () => {
    setWorkspace("free", "admin");
    render(<WorkspaceSleepReportsPage />);

    expect(screen.getByText("sleepReports.planGate.title")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "sleepReports.planGate.action" }),
    ).toBeNull();
  });

  it("free: shows the plan-gate copy with no action while /system/info is pending", () => {
    mockFeatures = null;
    setWorkspace("free");
    render(<WorkspaceSleepReportsPage />);

    expect(screen.getByText("sleepReports.planGate.title")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "sleepReports.planGate.action" }),
    ).toBeNull();
  });

  it("does not gate while WorkspaceContext is still loading", () => {
    mockWorkspaceState = {
      currentWorkspace: null,
      currentWorkspaceId: null,
      loading: true,
    };
    render(<WorkspaceSleepReportsPage />);
    expect(screen.queryByText("sleepReports.planGate.title")).toBeNull();
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("a tier with sleep_enabled_contexts_limit > 0 is not plan-gated even below pro (#1645)", () => {
    // An operator gives basic a sleep cap: the matrix, not the tier rank,
    // decides — the same predicate as the server's own gate.
    mockTiers = OSS_TIERS.map((t) =>
      t.name === "basic" ? { ...t, sleep_enabled_contexts_limit: 1 } : t,
    );
    setWorkspace("basic");
    render(<WorkspaceSleepReportsPage />);
    expect(screen.getByTestId("sleep-reports-list")).toBeInTheDocument();
    expect(screen.queryByText("sleepReports.planGate.title")).toBeNull();
  });

  it("neither gates nor loads reports while the tier matrix is still resolving (#1645)", () => {
    mockTiers = null;
    setWorkspace("free");
    render(<WorkspaceSleepReportsPage />);
    expect(screen.queryByText("sleepReports.planGate.title")).toBeNull();
    // The list holds its skeleton: no reports before the plan answer.
    expect(screen.getByTestId("sleep-reports-list")).toHaveAttribute(
      "data-ready",
      "false",
    );
  });

  it("an entitled tier loads the list once the gate answers (#1645)", () => {
    setWorkspace("pro");
    render(<WorkspaceSleepReportsPage />);
    expect(screen.getByTestId("sleep-reports-list")).toHaveAttribute(
      "data-ready",
      "true",
    );
  });

  it("role before plan: a member on a low tier gets the role refusal, not the upsell (#1645)", () => {
    setWorkspace("free", "member");
    render(<WorkspaceSleepReportsPage />);
    expect(
      screen.getByText("sleepReports.errors.forbiddenWorkspace"),
    ).toBeInTheDocument();
    expect(screen.queryByText("sleepReports.planGate.title")).toBeNull();
  });
});

// #1645: every cell of {tier matrix} x {/system/info} x {workspace, role,
// tier} as the page renders it. A failed matrix reads exactly like a pending
// one here (`usePlanTierMatrix` answers null for both; the hook suite pins
// that), and a failed /system/info is the fail-closed `{}`.
describe("WorkspaceSleepReportsPage — the whole gate truth table (#1645)", () => {
  type Ws =
    | { kind: "loading" }
    | { kind: "none" }
    | { kind: "resolved"; role: string; plan: string };
  const WORKSPACES: Ws[] = [
    { kind: "loading" },
    { kind: "none" },
    ...["viewer", "member", "admin", "owner"].flatMap((role) =>
      ["free", "pro"].map((plan): Ws => ({ kind: "resolved", role, plan })),
    ),
  ];
  const MATRIX: Record<string, PlanTierFeature[] | null> = {
    "pending-or-failed": null,
    resolved: OSS_TIERS,
  };
  const INFO: Record<string, Record<string, boolean> | null> = {
    pending: null,
    "plan_page on": { plan_page: true },
    "plan_page off": { plan_page: false },
    "failed ({})": {},
  };
  const CELLS = Object.keys(MATRIX).flatMap((m) =>
    Object.keys(INFO).flatMap((i) =>
      WORKSPACES.map((ws) => [m, i, ws] as const),
    ),
  );

  const label = (ws: Ws) =>
    ws.kind === "resolved" ? `${ws.role}@${ws.plan}` : ws.kind;

  it.each(CELLS.map(([m, i, ws]) => [m, i, label(ws), ws] as const))(
    "matrix %s, /system/info %s, workspace %s",
    (m, i, _label, ws) => {
      mockTiers = MATRIX[m];
      mockFeatures = INFO[i];
      mockWorkspaceState =
        ws.kind === "resolved"
          ? {
              currentWorkspace: {
                plan_name: ws.plan,
                current_user_role: ws.role,
              },
              currentWorkspaceId: "ws-1",
              loading: false,
            }
          : {
              currentWorkspace: null,
              currentWorkspaceId: null,
              loading: ws.kind === "loading",
            };
      render(<WorkspaceSleepReportsPage />);

      const upsell = screen.queryByText("sleepReports.planGate.title");
      const cta = screen.queryByRole("button", {
        name: "sleepReports.planGate.action",
      });
      const forbidden = screen.queryByText(
        "sleepReports.errors.forbiddenWorkspace",
      );
      const list = screen.queryByTestId("sleep-reports-list");

      const matrixKnown = m === "resolved";
      const isAdmin =
        ws.kind === "resolved" && ["admin", "owner"].includes(ws.role);
      const lowTier = ws.kind === "resolved" && ws.plan === "free";

      // The upsell: only once the matrix has answered, only to an admin or
      // owner (role outranks plan), only on a tier without Sleep Maintenance.
      expect(upsell !== null).toBe(matrixKnown && isAdmin && lowTier);
      // Its button: only for the owner, and only where the Plan page is
      // known to be on — never while /system/info is pending or failed.
      expect(cta !== null).toBe(
        upsell !== null &&
          ws.kind === "resolved" &&
          ws.role === "owner" &&
          i === "plan_page on",
      );
      // A member or viewer is refused on the role, whatever the matrix says.
      expect(forbidden !== null).toBe(ws.kind === "resolved" && !isAdmin);
      // No workspace at all is its own answer, never an upsell.
      if (ws.kind === "none") {
        expect(
          screen.getByText("sleepReports.errors.noWorkspaceSelected"),
        ).toBeInTheDocument();
      }
      // The list loads only for an entitled admin once the gate answers; while
      // anything is pending it holds its skeleton.
      if (list !== null) {
        expect(list.getAttribute("data-ready")).toBe(
          String(matrixKnown && isAdmin && !lowTier),
        );
      }
      if (matrixKnown && isAdmin && !lowTier) expect(list).not.toBeNull();
    },
  );
});
