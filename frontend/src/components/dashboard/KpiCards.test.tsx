import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { KpiCards } from "./KpiCards";
import type { ContextStatsResponse } from "@/lib/api/workspaces";

vi.mock("next-intl", () => ({
  useTranslations:
    (_ns: string) => (key: string, vars?: Record<string, unknown>) =>
      vars && Object.keys(vars).length > 0
        ? `${key}:${JSON.stringify(vars)}`
        : key,
}));

const mockContextStats: ContextStatsResponse = {
  contexts: [
    {
      context_id: "ctx-1",
      context_name: "dev",
      memory_count: 100,
      last_activity: "2026-04-10T00:00:00Z",
      member_count: 2,
      api_calls_week: 50,
      active_users_week: 3,
      avg_response_time_ms: 120,
    },
    {
      context_id: "ctx-2",
      context_name: "prod",
      memory_count: 200,
      last_activity: "2026-04-09T00:00:00Z",
      member_count: 5,
      api_calls_week: 150,
      active_users_week: 4,
      avg_response_time_ms: 80,
    },
  ],
  total_contexts: 2,
  workspace_totals: { memory_count: 300 },
};

describe("KpiCards", () => {
  it("renders all 4 KPI cards with correct values", () => {
    render(
      <KpiCards
        totalMemories={1234}
        contextCount={5}
        contextStats={mockContextStats}
      />,
    );

    expect(screen.getByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    // 50 + 150 = 200
    expect(screen.getByText("200")).toBeInTheDocument();
    // 3 + 4 = 7
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("renders all 4 i18n labels", () => {
    render(<KpiCards totalMemories={0} contextCount={0} contextStats={null} />);

    expect(screen.getByText("totalMemories")).toBeInTheDocument();
    expect(screen.getByText("contextCount")).toBeInTheDocument();
    expect(screen.getByText("apiCallsWeek")).toBeInTheDocument();
    expect(screen.getByText("activeUsersWeek")).toBeInTheDocument();
  });

  it("handles null contextStats gracefully", () => {
    render(<KpiCards totalMemories={0} contextCount={0} contextStats={null} />);

    // API calls and active users should show 0
    const zeros = screen.getAllByText("0");
    expect(zeros.length).toBeGreaterThanOrEqual(4);
  });
});

describe("unsearchable memories (#1496)", () => {
  /**
   * The Total Memories card counts rows, not searchability. A failed embedding
   * never reaches Qdrant — and BM25 lives there too — so those memories are
   * missing from recall in both modes while still being counted here and
   * charged against quota. 467 accumulated on production precisely because
   * every number the user could see agreed with every other one.
   *
   * The qualification belongs on this card because this is the number that is
   * misleading without it.
   */
  it("says nothing when every memory is searchable", () => {
    render(
      <KpiCards
        totalMemories={300}
        contextCount={2}
        contextStats={mockContextStats}
      />,
    );
    expect(screen.queryByText(/unsearchableSubtext/)).toBeNull();
  });

  it("qualifies the memory count when some are not searchable", () => {
    render(
      <KpiCards
        totalMemories={300}
        contextCount={2}
        contextStats={mockContextStats}
        unsearchableCount={418}
      />,
    );
    const subtext = screen.getByText(/unsearchableSubtext/);
    expect(subtext.textContent).toContain('"count":418');
  });

  it("defaults to silent when the backend does not send the field", () => {
    // An older backend during a blue/green rollout omits it entirely; the
    // dashboard must not imply everything is fine OR that anything is wrong.
    render(
      <KpiCards
        totalMemories={300}
        contextCount={2}
        contextStats={mockContextStats}
        unsearchableCount={undefined}
      />,
    );
    expect(screen.queryByText(/unsearchableSubtext/)).toBeNull();
  });
});
