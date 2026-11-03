/**
 * #2219: the Sleep Reports list shows each row's source (Sleep run or
 * Memory Analysis spend row) and filters on it.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import { SleepReportsList } from "./SleepReportsList";
import type {
  SleepReportListResponse,
  SleepReportSummary,
} from "@/lib/api/sleep-reports";
import { fetchAdminSleepReports } from "@/lib/api/sleep-reports";

const mockApiGet = vi.fn();

vi.mock("@/lib/api/base", () => ({
  apiClient: { get: (...args: unknown[]) => mockApiGet(...args) },
}));

const stableTranslator = (key: string, values?: Record<string, unknown>) =>
  values && Object.keys(values).length > 0
    ? `${key}:${JSON.stringify(values)}`
    : key;

vi.mock("next-intl", () => ({
  useTranslations: () => stableTranslator,
  useLocale: () => "en",
}));

vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
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

function report(overrides: Partial<SleepReportSummary>): SleepReportSummary {
  return {
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
    memories_processed: 1,
    edges_created: 0,
    memories_merged: 0,
    memories_promoted: 0,
    memories_flagged: 0,
    llm_calls_made: 0,
    llm_tokens_used: 0,
    ...overrides,
  };
}

function response(reports: SleepReportSummary[]): SleepReportListResponse {
  return { reports, total: reports.length, limit: 50, offset: 0 };
}

describe("SleepReportsList — source (#2219)", () => {
  const fetchData = vi.fn();

  beforeEach(() => {
    fetchData.mockReset();
    mockApiGet.mockReset();
  });

  it("shows a Source column with a badge per row", async () => {
    fetchData.mockResolvedValue(
      response([
        report({ id: "r-1", source: "sleep" }),
        report({ id: "r-2", source: "analysis" }),
      ]),
    );
    render(
      <SleepReportsList
        title="t"
        description="d"
        fetchData={fetchData}
        detailHrefPrefix="/admin/sleep-reports"
      />,
    );

    expect(
      await screen.findByRole("columnheader", { name: "table.source" }),
    ).toBeInTheDocument();
    const badges = screen.getAllByTestId("sleep-report-source");
    expect(badges.map((b) => b.textContent)).toEqual([
      "source.sleep",
      "source.analysis",
    ]);
  });

  it("asks for every source by default and narrows on a source filter", async () => {
    fetchData.mockResolvedValue(response([report({})]));
    render(
      <SleepReportsList
        title="t"
        description="d"
        fetchData={fetchData}
        detailHrefPrefix="/admin/sleep-reports"
      />,
    );

    await waitFor(() => expect(fetchData).toHaveBeenCalledTimes(1));
    expect(fetchData.mock.calls[0][0].source).toBeUndefined();
    const all = screen.getByRole("button", { name: "filter.sourceAll" });
    expect(all).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(
      screen.getByRole("button", { name: "source.analysis" }),
    );

    await waitFor(() => expect(fetchData).toHaveBeenCalledTimes(2));
    expect(fetchData.mock.calls[1][0]).toMatchObject({
      source: "analysis",
      offset: 0,
    });
    expect(
      screen.getByRole("button", { name: "source.analysis" }),
    ).toHaveAttribute("aria-pressed", "true");
  });
});

describe("sleep-reports API client — source (#2219)", () => {
  beforeEach(() => mockApiGet.mockReset());

  it("sends source only when a filter is set", async () => {
    mockApiGet.mockResolvedValue(response([]));

    await fetchAdminSleepReports({ source: "sleep", limit: 50 });
    await fetchAdminSleepReports({ limit: 50 });

    expect(mockApiGet.mock.calls[0][0]).toContain("source=sleep");
    expect(mockApiGet.mock.calls[1][0]).not.toContain("source=");
  });
});
