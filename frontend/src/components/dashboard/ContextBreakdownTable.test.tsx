import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
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

const ORIGINAL_URL = {
  createObjectURL: URL.createObjectURL,
  revokeObjectURL: URL.revokeObjectURL,
};
afterEach(() => {
  vi.restoreAllMocks();
  Object.defineProperty(URL, "createObjectURL", {
    value: ORIGINAL_URL.createObjectURL,
    configurable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    value: ORIGINAL_URL.revokeObjectURL,
    configurable: true,
  });
});

/**
 * Stubs the download path and returns the CSV text the next export writes.
 * Restored in afterEach.
 */
function captureCsvExport(): () => Promise<string> {
  const blobs: Blob[] = [];
  Object.defineProperty(URL, "createObjectURL", {
    value: (b: Blob) => {
      blobs.push(b);
      return "blob:csv";
    },
    configurable: true,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    value: vi.fn(),
    configurable: true,
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  return () => {
    expect(blobs).toHaveLength(1);
    return blobs[0].text();
  };
}

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
        currentUserId="viewer"
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

  // ---------- Issue #1755: visibility text, "You" as owner, CSV columns ----

  it("labels each row's visibility with text next to the icon", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
      />,
    );
    const rowOf = (name: string) =>
      screen.getByText(name).closest("tr") as HTMLTableRowElement;
    expect(rowOf("dev")).toHaveTextContent("sharedContext");
    expect(rowOf("prod")).toHaveTextContent("privateContext");
  });

  it("attributes no owner while the viewer is unknown (auth hydrating)", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
      />,
    );
    fireEvent.click(screen.getByText("showDetails"));
    expect(screen.queryByText("Alice")).toBeNull();
    expect(screen.queryByText("ownerYou")).toBeNull();
  });

  it("shows You in the Owner column for the current user's contexts", () => {
    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
        currentUserId="user-2"
      />,
    );
    fireEvent.click(screen.getByText("showDetails"));
    const rowOf = (name: string) =>
      screen.getByText(name).closest("tr") as HTMLTableRowElement;
    expect(rowOf("prod")).toHaveTextContent("ownerYou");
    expect(rowOf("prod")).not.toHaveTextContent("Bob");
    expect(rowOf("dev")).toHaveTextContent("Alice");
  });

  it("exports Owner and Visibility columns in the CSV", () => {
    const exported = captureCsvExport();

    render(
      <ContextBreakdownTable
        contexts={mockContexts}
        totalMemories={300}
        contextStats={mockContextStats}
        currentUserId="user-1"
      />,
    );
    fireEvent.click(screen.getByText(/Export CSV/));

    return exported().then((csv) => {
      const lines = csv.split("\n");
      expect(lines[0]).toBe(
        "Context Name,Memory Count,Last Activity,Members,Owner,Visibility",
      );
      // Locale-neutral: the creator's name, never the localized "You".
      expect(lines[1]).toBe(
        '"dev","100","2026-04-10T00:00:00Z","2","Alice","Shared"',
      );
      expect(lines[2]).toBe(
        '"prod","200","2026-04-09T00:00:00Z","5","Bob","Private"',
      );
    });
  });

  it("escapes quotes and neutralises leading formula characters in CSV cells", () => {
    const exported = captureCsvExport();

    render(
      <ContextBreakdownTable
        contexts={[
          {
            context_id: "ctx-x",
            context_name: '=HYPERLINK("x")',
            created_by: "u",
            created_by_name: 'Bob "B" Smith',
            memory_count: 1,
            is_private: false,
          },
        ]}
        totalMemories={1}
        contextStats={{
          contexts: [
            {
              context_id: "ctx-x",
              context_name: '=HYPERLINK("x")',
              memory_count: 1,
              last_activity: null,
              member_count: 1,
              api_calls_week: 0,
              active_users_week: 0,
              avg_response_time_ms: 0,
            },
          ],
          total_contexts: 1,
          workspace_totals: { memory_count: 1 },
        }}
      />,
    );
    fireEvent.click(screen.getByText(/Export CSV/));

    return exported().then((csv) => {
      expect(csv.split("\n")[1]).toBe(
        `"'=HYPERLINK(""x"")","1","Never","1","Bob ""B"" Smith","Shared"`,
      );
    });
  });

  it.each(["+SUM(1)", "-1", "@cmd", "\tTabbed", "\rCR"])(
    "prefixes a cell starting with a formula character (%j) with a quote",
    (name) => {
      const exported = captureCsvExport();
      render(
        <ContextBreakdownTable
          contexts={[
            {
              context_id: "c",
              context_name: "ctx",
              created_by: "u",
              created_by_name: name,
              memory_count: 0,
              is_private: true,
            },
          ]}
          totalMemories={0}
          contextStats={{
            contexts: [
              {
                context_id: "c",
                context_name: "ctx",
                memory_count: 0,
                last_activity: null,
                member_count: 0,
                api_calls_week: 0,
                active_users_week: 0,
                avg_response_time_ms: 0,
              },
            ],
            total_contexts: 1,
            workspace_totals: { memory_count: 0 },
          }}
        />,
      );
      fireEvent.click(screen.getByText(/Export CSV/));
      return exported().then((csv) => {
        expect(csv.split("\n")[1]).toContain(`"'${name}"`);
      });
    },
  );
});
