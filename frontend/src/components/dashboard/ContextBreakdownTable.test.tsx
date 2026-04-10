import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ContextBreakdownTable } from "./ContextBreakdownTable";
import type { ContextStatsResponse } from "@/lib/api/workspaces";

vi.mock("next-intl", () => ({
  useTranslations: (_ns: string) => (key: string) => key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: () => "3h ago",
}));

const mockContexts = [
  {
    context_id: "ctx-1",
    context_name: "dev",
    created_by: "user-1",
    created_by_name: "Alice",
    memory_count: 100,
    is_private: false,
  },
  {
    context_id: "ctx-2",
    context_name: "prod",
    created_by: "user-2",
    created_by_name: "Bob",
    memory_count: 200,
    is_private: true,
  },
];

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

describe("ContextBreakdownTable", () => {
  it("renders 3 default columns (name, memories, last activity)", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
      />,
    );

    // Context names rendered as links
    expect(screen.getByText("dev")).toBeInTheDocument();
    expect(screen.getByText("prod")).toBeInTheDocument();

    // Memory counts
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("200")).toBeInTheDocument();

    // Detail columns should NOT be visible by default
    expect(screen.queryByText("owner")).not.toBeInTheDocument();
  });

  it("shows all 8 columns when Show Details is toggled", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
      />,
    );

    // Click "Show Details"
    fireEvent.click(screen.getByText("showDetails"));

    // Detail columns now visible (i18n keys as text)
    expect(screen.getByText("owner")).toBeInTheDocument();
    expect(screen.getByText("apiCallsWeek")).toBeInTheDocument();
    expect(screen.getByText("activeUsersWeek")).toBeInTheDocument();
    expect(screen.getByText("members")).toBeInTheDocument();
    expect(screen.getByText("percentOfTotal")).toBeInTheDocument();

    // Owner name visible
    expect(screen.getByText("Alice")).toBeInTheDocument();
  });

  it("toggles back to 3 columns when Hide Details is clicked", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
      />,
    );

    fireEvent.click(screen.getByText("showDetails"));
    expect(screen.getByText("owner")).toBeInTheDocument();

    fireEvent.click(screen.getByText("hideDetails"));
    expect(screen.queryByText("owner")).not.toBeInTheDocument();
  });

  it("renders empty state when no contexts", () => {
    render(
      <ContextBreakdownTable
        contexts={[]}
        totalMemories={0}
        contextStats={null}
      />,
    );

    expect(screen.getByText("noContextsFound")).toBeInTheDocument();
  });

  it("renders private aggregation row when present", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={500}
        privateAggregation={{ context_count: 3, memory_count: 150 }}
        contextStats={mockContextStats}
      />,
    );

    expect(screen.getByText("othersPrivate")).toBeInTheDocument();
    expect(screen.getByText("150")).toBeInTheDocument();
  });

  it("changes sort order when clicking column headers", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
      />,
    );

    // Default sort is by memory desc — prod (200) should be first
    const rows = screen.getAllByRole("row");
    // Row 0 is header, row 1 should be prod (200), row 2 dev (100)
    expect(rows[1]).toHaveTextContent("prod");
    expect(rows[2]).toHaveTextContent("dev");

    // Click name header to sort by name
    fireEvent.click(screen.getByText("contextName"));

    const rowsAfterSort = screen.getAllByRole("row");
    // Desc by name: prod > dev
    expect(rowsAfterSort[1]).toHaveTextContent("prod");
  });
});
