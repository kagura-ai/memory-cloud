/**
 * AnalysesTabPanel — cost display gate (#1571) and the refusal split (#1644).
 *
 * The "Run cost" KPI is money: it renders only when GET /system/info says
 * ``features.cost_display`` is true (fail-closed while loading), and the same
 * answer is handed to the history table and the new-run modal, which own the
 * other two money cells.
 *
 * A refused bootstrap renders one of three empty states — owner-only, the
 * required plan, or the plan-neutral allowlist copy — decided by the
 * normalised gate on the ApiError, never by the bare status. #1646: each is
 * the gate notice's page variant, so the copy is `gate.*` ("role.owner.title",
 * "plan.title", "allowlist.title" under the echo translator below).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";

// Stable identities: the panel's ``bootstrap`` useCallback depends on ``t``
// and the URL helpers, so a fresh function per render would re-fire the
// bootstrap effect on every render (a real next-intl ``t`` is stable).
// ICU params are echoed after the key so the plan label's flow is visible.
vi.mock("next-intl", () => {
  const t = (key: string, params?: Record<string, unknown>) =>
    params ? `${key} ${JSON.stringify(params)}` : key;
  return { useTranslations: () => t, useLocale: () => "en" };
});

// `push` is the gate notice's upgrade CTA (#1646).
const mockRouter = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => {
  const params = new URLSearchParams();
  return {
    useRouter: () => mockRouter,
    usePathname: () => "/workspace/contexts/ctx-1",
    useSearchParams: () => params,
  };
});

let mockFeatures: Record<string, boolean> | null = { cost_display: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
}));

// #1646: the upgrade answer (useCanUpgrade) reads the member's role.
let mockRole = "owner";
vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({
    currentWorkspace: { id: "w1", current_user_role: mockRole },
    loading: false,
  }),
}));

const fixtures = vi.hoisted(() => {
  const run = {
    run_id: "run-1",
    workspace_id: "w1",
    context_id: "ctx-1",
    status: "succeeded",
    triggered_by: "u1",
    started_at: "2026-05-01T00:00:00Z",
    finished_at: "2026-05-01T00:05:00Z",
    input_count: 10,
    cost_estimated_cents: 5,
    cost_actual_cents: 4,
    error: null,
    cancellation_reason: null,
  };
  const cluster = {
    cluster_index: 0,
    label: "Cluster A",
    description: null,
    count: 10,
    centroid_2d: [0, 0],
    label_confidence: 0.9,
    representative_memory_ids: [],
    property_stats: {},
  };
  return { run, cluster };
});

vi.mock("@/lib/api/analyses", () => ({
  getActiveAnalysis: vi.fn().mockResolvedValue(fixtures.run),
  listAnalysisRuns: vi
    .fn()
    .mockResolvedValue({ items: [fixtures.run], next_cursor: null }),
  listRunClusters: vi.fn().mockResolvedValue({ items: [fixtures.cluster] }),
  listRunPositions: vi.fn().mockResolvedValue({ items: [] }),
  getAnalysisRun: vi.fn(),
  cancelAnalysisRun: vi.fn(),
}));

vi.mock("./useActiveAnalysisPolling", () => ({
  useActiveAnalysisPolling: () => ({ run: null, refetch: vi.fn() }),
}));
vi.mock("./useFocusedClusterId", () => ({
  useFocusedClusterId: () => ({
    focusedClusterId: null,
    setFocusedClusterId: vi.fn(),
    toggleFocusedClusterId: vi.fn(),
  }),
}));
// Heavy children are out of scope here; the two that own money cells echo
// the ``showCost`` prop so the wiring is asserted.
vi.mock("./ScatterPlot", () => ({ ScatterPlot: () => null }));
vi.mock("./ClusterList", () => ({ ClusterList: () => null }));
vi.mock("./RepresentativesPanel", () => ({ RepresentativesPanel: () => null }));
vi.mock("./PropertyStats", () => ({ PropertyStats: () => null }));
vi.mock("./AnalysisHistory", () => ({
  AnalysisHistory: ({ showCost }: { showCost: boolean }) => (
    <div data-testid="analysis-history" data-show-cost={String(showCost)} />
  ),
}));
vi.mock("./NewAnalysisModal", () => ({
  NewAnalysisModal: ({ showCost }: { showCost: boolean }) => (
    <div data-testid="new-analysis-modal" data-show-cost={String(showCost)} />
  ),
}));

import { getActiveAnalysis, listAnalysisRuns } from "@/lib/api/analyses";
import { ApiError } from "@/lib/api/base";
import { normalizeGate } from "@/lib/gates/featureGates";
import { AnalysesTabPanel } from "./AnalysesTabPanel";

beforeEach(() => {
  mockFeatures = { cost_display: true };
  mockRole = "owner";
  mockRouter.push.mockReset();
});

afterEach(() => cleanup());

async function renderPanel() {
  render(<AnalysesTabPanel contextId="ctx-1" contextName="My context" />);
  // Bootstrap resolved: the history table (mocked) is on screen.
  return screen.findByTestId("analysis-history");
}

describe("AnalysesTabPanel — cost display gate (#1571)", () => {
  it("renders the Run cost KPI and passes showCost=true down when the flag is on", async () => {
    const history = await renderPanel();
    expect(screen.getByText("kpi.runCost")).toBeInTheDocument();
    expect(screen.getByText("$0.040")).toBeInTheDocument();
    expect(history).toHaveAttribute("data-show-cost", "true");
    expect(screen.getByTestId("new-analysis-modal")).toHaveAttribute(
      "data-show-cost",
      "true",
    );
  });

  it("renders no Run cost KPI and passes showCost=false down when the flag is off", async () => {
    mockFeatures = { cost_display: false };
    const history = await renderPanel();
    expect(screen.queryByText("kpi.runCost")).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
    // The non-money KPIs are untouched.
    expect(screen.getByText("kpi.memoriesSurveyed")).toBeInTheDocument();
    expect(history).toHaveAttribute("data-show-cost", "false");
    expect(screen.getByTestId("new-analysis-modal")).toHaveAttribute(
      "data-show-cost",
      "false",
    );
  });

  it("fails closed while /system/info is still loading", async () => {
    mockFeatures = null;
    await renderPanel();
    expect(screen.queryByText("kpi.runCost")).toBeNull();
  });
});

describe("AnalysesTabPanel — refusal split (#1644)", () => {
  /** An ApiError exactly as lib/api/base.ts would build it from this body. */
  function refusal(
    status: number,
    error: string | undefined,
    details: Record<string, unknown>,
  ): ApiError {
    return new ApiError({
      error,
      message: "refused",
      status,
      details,
      gate: normalizeGate(status, error, details),
    });
  }

  /** Both bootstrap reads refuse the same way (they share the read gate). */
  function refuseBoth(err: unknown) {
    vi.mocked(getActiveAnalysis).mockRejectedValueOnce(err);
    vi.mocked(listAnalysisRuns).mockRejectedValueOnce(err);
  }

  function renderRefused() {
    render(<AnalysesTabPanel contextId="ctx-1" contextName="My context" />);
  }

  const PLAN_ACTION = /^plan\.action /;

  it("shows the owner-only state for an AUTH-101 role refusal, not 'not yet enabled'", async () => {
    refuseBoth(refusal(403, "AUTH-101", {}));
    renderRefused();

    expect(await screen.findByText(/^role\.owner\.title/)).toBeInTheDocument();
    expect(
      screen.getByText(/^role\.owner\.description .*memory_analysis/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^allowlist\./)).toBeNull();
    expect(screen.queryByText(/^plan\./)).toBeNull();
  });

  it("names the required plan for a FEAT-001 plan refusal, not 'not yet enabled'", async () => {
    refuseBoth(
      refusal(403, "FEAT-001", {
        gate: "plan",
        feature: "memory_analysis",
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "basic",
      }),
    );
    renderRefused();

    // The whole feature ("all" scope), the tier the refusal names.
    expect(
      await screen.findByText(/^plan\.title .*"plan":"L"/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/^plan\.description .*"plan":"L"/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^allowlist\./)).toBeNull();
    expect(screen.queryByText(/^role\./)).toBeNull();
    // No Plan page on this deployment: no CTA.
    expect(screen.queryByRole("button", { name: PLAN_ACTION })).toBeNull();
  });

  it("a plan refusal offers the upgrade to an owner on a Plan-page deployment (#1646)", async () => {
    mockFeatures = { cost_display: true, plan_page: true };
    refuseBoth(
      refusal(403, "FEAT-001", {
        gate: "plan",
        feature: "memory_analysis",
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "basic",
      }),
    );
    renderRefused();

    fireEvent.click(
      await screen.findByRole("button", { name: /^plan\.action .*"plan":"L"/ }),
    );
    expect(mockRouter.push).toHaveBeenCalledWith("/workspace/settings/plan");
  });

  it("the plan refusal's CTA is the owner's alone (#1646)", async () => {
    mockFeatures = { cost_display: true, plan_page: true };
    mockRole = "admin";
    refuseBoth(
      refusal(403, "FEAT-001", {
        gate: "plan",
        feature: "memory_analysis",
        required_plan: "pro",
        required_plan_display: "L",
        current_plan: "basic",
      }),
    );
    renderRefused();

    expect(await screen.findByText(/^plan\.title/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: PLAN_ACTION })).toBeNull();
  });

  it("keeps the plan-neutral, CTA-free copy for an allowlist refusal", async () => {
    // #1646: even for an owner on a Plan-page deployment — money does not
    // lift a rollout gate (A4).
    mockFeatures = { cost_display: true, plan_page: true };
    // Wire-identical to the plan refusal except for details.gate.
    refuseBoth(
      refusal(403, "FEAT-001", {
        gate: "allowlist",
        feature: "memory_analysis",
        required_plan: null,
        required_plan_display: null,
        current_plan: "pro",
      }),
    );
    renderRefused();

    expect(await screen.findByText(/^allowlist\.title/)).toBeInTheDocument();
    expect(screen.getByText(/^allowlist\.description/)).toBeInTheDocument();
    expect(screen.queryByText(/^plan\./)).toBeNull();
    expect(screen.queryByText(/^role\./)).toBeNull();
    // Plan-neutral: no tier is ever interpolated.
    expect(document.body.textContent).not.toMatch(/"(plan|currentPlan)":/);
    // No upgrade path of any kind.
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("keeps today's copy for a bare 403 that carries no gate", async () => {
    refuseBoth(refusal(403, "HTTP-403", { detail: "Forbidden" }));
    renderRefused();

    // #1644's bare-403 branch renders the same gate.allowlist copy (C-9).
    expect(await screen.findByText(/^allowlist\.title/)).toBeInTheDocument();
    expect(screen.getByText(/^allowlist\.description/)).toBeInTheDocument();
    expect(screen.queryByText(/^role\./)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("degrades an older server's allowlist refusal (FEAT-001, no gate) to the plan-neutral copy", async () => {
    // An owner on a Plan-page deployment: a guessed tier would be an
    // upgrade the backend will not honour.
    mockFeatures = { cost_display: true, plan_page: true };
    refuseBoth(refusal(403, "FEAT-001", { feature: "memory_analysis" }));
    renderRefused();

    expect(await screen.findByText(/^allowlist\.title/)).toBeInTheDocument();
    expect(screen.queryByText(/^plan\./)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("uses the history refusal when there is no active run", async () => {
    vi.mocked(getActiveAnalysis).mockRejectedValueOnce(
      new ApiError({ message: "No active run", status: 404 }),
    );
    vi.mocked(listAnalysisRuns).mockRejectedValueOnce(
      refusal(403, "AUTH-101", {}),
    );
    renderRefused();

    expect(await screen.findByText(/^role\.owner\.title/)).toBeInTheDocument();
  });

  it("does not treat a non-403 gate as a panel refusal", async () => {
    // A daily REST quota 429 is a real gate, but not one the panel replaces
    // itself with: it stays an error.
    refuseBoth(
      refusal(429, "QUOTA-001", {
        gate: "quota",
        quota_type: "api_rest_daily",
        retry_after: 86400,
      }),
    );
    renderRefused();

    expect(await screen.findByText("refused")).toBeInTheDocument();
    expect(screen.queryByText(/^allowlist\./)).toBeNull();
  });
});
