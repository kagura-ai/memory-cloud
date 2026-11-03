/**
 * #2219: the detail view names the row's source, and a Memory Analysis spend
 * row says why it has no phase results instead of listing five empty Sleep
 * phases.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { SleepReportDetailView } from "./SleepReportDetailView";
import type {
  SleepReportDetail,
  SleepReportDetailResponse,
} from "@/lib/api/sleep-reports";

const stableTranslator = (key: string, values?: Record<string, unknown>) =>
  values && Object.keys(values).length > 0
    ? `${key}:${JSON.stringify(values)}`
    : key;

vi.mock("next-intl", () => ({
  useTranslations: () => stableTranslator,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

vi.mock("next/link", () => ({
  default: ({
    children,
    href,
  }: {
    children: React.ReactNode;
    href: string;
  }) => <a href={href}>{children}</a>,
}));

function detail(overrides: Partial<SleepReportDetail>): SleepReportDetailResponse {
  return {
    report: {
      id: "r-1",
      user_id: "u-1",
      workspace_id: "w-1",
      context_id: "c-1",
      context_name: "ctx",
      user_email: "a@example.com",
      status: "completed",
      source: "sleep",
      started_at: "2026-11-01T00:00:00Z",
      completed_at: "2026-11-01T00:01:00Z",
      memories_processed: 3,
      edges_created: 0,
      memories_merged: 0,
      memories_promoted: 0,
      memories_flagged: 0,
      llm_calls_made: 4,
      llm_tokens_used: 900,
      context_deleted: false,
      embedding_calls_made: 0,
      error_message: null,
      edge_discovery_result: null,
      dedup_result: null,
      importance_result: null,
      consolidation_result: null,
      reindex_result: null,
      ...overrides,
    },
    actions: [],
    action_count: 0,
  };
}

function renderDetail(overrides: Partial<SleepReportDetail>) {
  return render(
    <SleepReportDetailView
      detail={detail(overrides)}
      backHref="/admin/sleep-reports"
      backLabel="back"
    />,
  );
}

describe("SleepReportDetailView — source (#2219)", () => {
  it("shows a Sleep run's source and its five phases", () => {
    renderDetail({ source: "sleep" });

    expect(screen.getByTestId("sleep-report-source")).toHaveTextContent(
      "source.sleep",
    );
    expect(screen.getByText("detail.edgeDiscovery")).toBeInTheDocument();
    expect(screen.getByText("detail.reindex")).toBeInTheDocument();
    expect(screen.queryByText("detail.analysisNoPhases")).toBeNull();
  });

  it("says an analysis row has no phase results instead of listing empty Sleep phases", () => {
    renderDetail({ source: "analysis" });

    expect(screen.getByTestId("sleep-report-source")).toHaveTextContent(
      "source.analysis",
    );
    expect(screen.getByText("detail.analysisNoPhases")).toBeInTheDocument();
    expect(screen.queryByText("detail.edgeDiscovery")).toBeNull();
    expect(screen.queryByText("detail.reindex")).toBeNull();
    // The Sleep headline (merged / edges / promoted) does not describe an
    // analysis row; its own headline counts the LLM spend.
    expect(
      screen.getByText(/^detail\.narrative\.headlineAnalysis/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^detail\.narrative\.headline:/)).toBeNull();
  });

  it("renders an analysis row whose phase result fields are missing", () => {
    const missing = {
      source: "analysis",
      edge_discovery_result: undefined,
      dedup_result: undefined,
      importance_result: undefined,
      consolidation_result: undefined,
      reindex_result: undefined,
    } as unknown as Partial<SleepReportDetail>;
    renderDetail(missing);

    expect(screen.getByText("detail.analysisNoPhases")).toBeInTheDocument();
  });

  it("does not show the Sleep judge warning on a degraded analysis row", () => {
    renderDetail({
      source: "analysis",
      status: "degraded",
      llm_call_failures: 2,
    });

    // The Sleep text blames merge/importance judgments and points at phase
    // results the analysis row does not show.
    expect(
      screen.queryByText(/^detail\.narrative\.runDegraded(Phase)?:/),
    ).toBeNull();
    expect(
      screen.getByText(/^detail\.narrative\.runDegradedAnalysis/),
    ).toHaveTextContent('"count":2');
  });

  it("keeps the Sleep judge warning on a degraded Sleep run", () => {
    renderDetail({ source: "sleep", status: "degraded", llm_call_failures: 2 });

    expect(
      screen.getByText(/^detail\.narrative\.runDegraded:/),
    ).toBeInTheDocument();
  });
});
