/**
 * AnalysesTabPanel — cost display gate (#1571).
 *
 * The "Run cost" KPI is money: it renders only when GET /system/info says
 * ``features.cost_display`` is true (fail-closed while loading), and the same
 * answer is handed to the history table and the new-run modal, which own the
 * other two money cells.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

// Stable identities: the panel's ``bootstrap`` useCallback depends on ``t``
// and the URL helpers, so a fresh function per render would re-fire the
// bootstrap effect on every render (a real next-intl ``t`` is stable).
vi.mock("next-intl", () => {
  const t = (key: string) => key;
  return { useTranslations: () => t };
});

vi.mock("next/navigation", () => {
  const router = { replace: vi.fn() };
  const params = new URLSearchParams();
  return {
    useRouter: () => router,
    usePathname: () => "/workspace/contexts/ctx-1",
    useSearchParams: () => params,
  };
});

let mockFeatures: Record<string, boolean> | null = { cost_display: true };
vi.mock("@/hooks/useSystemFeatures", () => ({
  useSystemFeatures: () => mockFeatures,
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

import { AnalysesTabPanel } from "./AnalysesTabPanel";

beforeEach(() => {
  mockFeatures = { cost_display: true };
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
