/**
 * PublicEndpointUsage (#3035): the owner card for the public MCP endpoint.
 *
 * Rendered with the REAL en / ja catalogues so a missing key or ICU argument
 * fails here; `afterEach` fails the test on any error the provider reports.
 * Recharts is replaced by a pass-through (happy-dom has no layout).
 */

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import { ApiError } from "@/lib/api/base";
import type { PublicMcpUsageStatsResponse } from "@/lib/api/resources";

const mockGetStats = vi.fn();
vi.mock("@/lib/api/resources", () => ({
  getPublicMcpUsageStats: (...a: unknown[]) => mockGetStats(...a),
}));

vi.mock("recharts", () => {
  const Pass = ({ children }: { children?: ReactNode }) => (
    <div data-testid="chart">{children}</div>
  );
  const Nothing = () => null;
  return {
    ResponsiveContainer: Pass,
    LineChart: Pass,
    Line: Nothing,
    XAxis: Nothing,
    YAxis: Nothing,
    Tooltip: Nothing,
    CartesianGrid: Nothing,
    Legend: Nothing,
  };
});

import {
  PublicEndpointUsage,
  shouldShowPublicEndpointUsage,
} from "./PublicEndpointUsage";

const CATALOGUES = { en, ja } as const;
type Locale = keyof typeof CATALOGUES;

let intlErrors: string[] = [];

beforeEach(() => {
  intlErrors = [];
  mockGetStats.mockReset();
});

afterEach(() => {
  cleanup();
  expect(intlErrors).toEqual([]);
});

function renderIn(locale: Locale, ui: ReactElement) {
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={CATALOGUES[locale] as Record<string, unknown>}
      timeZone="UTC"
      onError={(error) => {
        intlErrors.push(`${locale} ${error.code}: ${error.message}`);
      }}
    >
      {ui}
    </NextIntlClientProvider>,
  );
}

const OUTCOMES = {
  success: 0,
  degraded: 0,
  denied: 0,
  rate_limited: 0,
  invalid_request: 0,
  error: 0,
  cancelled: 0,
};

function stats(
  over: Partial<PublicMcpUsageStatsResponse> = {},
): PublicMcpUsageStatsResponse {
  return {
    resource_id: "catalog",
    endpoint_id: "00000000-0000-0000-0000-000000000001",
    window_days: 7,
    first_day: "2026-11-21",
    last_day: "2026-11-27",
    retention_days: 90,
    recording_enabled: true,
    totals: {
      calls: 42,
      searches: 30,
      search_outcomes: { ...OUTCOMES, success: 25, degraded: 3, rate_limited: 2 },
    },
    days: [
      {
        date: "2026-11-21",
        calls: 42,
        searches: 30,
        search_outcomes: { ...OUTCOMES, success: 25, degraded: 3, rate_limited: 2 },
      },
    ],
    documents: {
      status: "ok",
      publication_serving: true,
      searches_scanned: 28,
      scan_limit: 20000,
      items: [
        {
          doc_key: "a".repeat(32),
          searches: 12,
          best_rank: 1,
          title: "Trail shoe",
          source_url: "https://example.com/p/1",
        },
        {
          doc_key: "b".repeat(32),
          searches: 3,
          best_rank: 4,
          title: null,
          source_url: null,
        },
      ],
    },
    generated_at: "2026-11-27T10:00:00Z",
    ...over,
  };
}

async function show(locale: Locale = "en", refreshKey = 0) {
  const view = renderIn(
    locale,
    <PublicEndpointUsage resourceId="catalog" refreshKey={refreshKey} />,
  );
  await waitFor(() => expect(mockGetStats).toHaveBeenCalled());
  return view;
}

describe("PublicEndpointUsage", () => {
  it("shows exactly three tiles with the totals", async () => {
    mockGetStats.mockResolvedValue(stats());
    await show();
    const tiles = await screen.findAllByTestId("public-endpoint-tile");
    expect(tiles.map((t) => t.dataset.label)).toEqual([
      "Calls",
      "Searches",
      "Searches answered",
    ]);
    expect(tiles.map((t) => t.dataset.value)).toEqual(["42", "30", "28"]);
    expect(mockGetStats).toHaveBeenCalledWith("catalog", 7);
  });

  it("lists the non-zero outcomes with their labels", async () => {
    mockGetStats.mockResolvedValue(stats());
    await show();
    const list = await screen.findByTestId("public-endpoint-outcomes");
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["Answered25", "Answered (degraded)3", "Rate limited2"]);
  });

  it("links a document title and labels one no longer published", async () => {
    mockGetStats.mockResolvedValue(stats());
    await show();
    const link = await screen.findByRole("link", { name: "Trail shoe" });
    expect(link).toHaveAttribute("href", "https://example.com/p/1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("No longer published")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("12")).toBeInTheDocument();
    expect(within(table).getByText("4")).toBeInTheDocument();
  });

  it("shows no table while the endpoint is not serving", async () => {
    mockGetStats.mockResolvedValue(
      stats({
        documents: {
          ...stats().documents,
          publication_serving: false,
          items: stats().documents.items.map((i) => ({
            ...i,
            title: null,
            source_url: null,
          })),
        },
      }),
    );
    await show();
    expect(
      await screen.findByText(
        "The endpoint is not serving right now, so document titles are not shown.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText("No longer published")).not.toBeInTheDocument();
  });

  it("shows the unavailable line, and not the not-serving line, when documents failed", async () => {
    mockGetStats.mockResolvedValue(
      stats({
        documents: {
          status: "unavailable",
          publication_serving: false,
          searches_scanned: 0,
          scan_limit: 20000,
          items: [],
        },
      }),
    );
    await show();
    expect(
      await screen.findByText(/could not be read right now/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/not serving right now/)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("public-endpoint-tile")).toHaveLength(3);
  });

  it("says when the scan limit was reached and when recording is off", async () => {
    mockGetStats.mockResolvedValue(
      stats({
        recording_enabled: false,
        documents: {
          ...stats().documents,
          searches_scanned: 20000,
        },
      }),
    );
    await show();
    expect(
      await screen.findByText(
        "This list is based on the most recent 20,000 searches of the period.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/recording is switched off/)).toBeInTheDocument();
  });

  it.each(["en", "ja"] as const)(
    "renders the five notes in %s with the retention and section name",
    async (locale) => {
      mockGetStats.mockResolvedValue(stats({ retention_days: 45 }));
      await show(locale);
      const notes = await screen.findByTestId("public-endpoint-notes");
      const items = within(notes).getAllByRole("listitem");
      expect(items).toHaveLength(5);
      const text = notes.textContent ?? "";
      const t = CATALOGUES[locale].contextStats;
      expect(text).toContain(t.publicAPIUsage);
      expect(text).toContain("45");
      const cache =
        locale === "en"
          ? "Answers served from a cache in front of the endpoint are not included."
          : "エンドポイントの手前にあるキャッシュが返した応答は含まれません。";
      expect(text).toContain(cache);
      expect(text).toContain(t.publicEndpoint.callsNote);
      expect(text).toContain(t.publicEndpoint.undercountNote);
    },
  );

  it("shows the rate-limited line on 429", async () => {
    mockGetStats.mockRejectedValue(
      new ApiError({ status: 429, error: "RATE-001", message: "x" }),
    );
    await show();
    expect(
      await screen.findByText(
        "Too many requests for these statistics. Try again in a minute.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Failed to load the endpoint usage statistics."),
    ).not.toBeInTheDocument();
  });

  it("shows an error banner on a failed load, 503 included", async () => {
    mockGetStats.mockRejectedValue(
      new ApiError({
        status: 503,
        error: "usage_stats_unavailable",
        message: "x",
      }),
    );
    await show();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Failed to load the endpoint usage statistics.",
    );
  });

  it("renders nothing on 404", async () => {
    mockGetStats.mockRejectedValue(
      new ApiError({ status: 404, error: "RES-001", message: "x" }),
    );
    const { container } = await show();
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("shows the empty state when the period has no call", async () => {
    mockGetStats.mockResolvedValue(
      stats({
        totals: { calls: 0, searches: 0, search_outcomes: { ...OUTCOMES } },
        days: [],
        documents: { ...stats().documents, searches_scanned: 0, items: [] },
      }),
    );
    await show();
    expect(
      await screen.findByText("No calls in this period"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("public-endpoint-tile")).not.toBeInTheDocument();
  });

  it("requests 30 days when the period changes", async () => {
    mockGetStats.mockResolvedValue(stats());
    await show();
    await screen.findAllByTestId("public-endpoint-tile");
    fireEvent.click(screen.getByRole("button", { name: "30d" }));
    await waitFor(() =>
      expect(mockGetStats).toHaveBeenLastCalledWith("catalog", 30),
    );
  });

  it("reloads when the refresh key changes", async () => {
    mockGetStats.mockResolvedValue(stats());
    const view = await show("en", 0);
    await screen.findAllByTestId("public-endpoint-tile");
    expect(mockGetStats).toHaveBeenCalledTimes(1);
    await act(async () => {
      view.rerender(
        <NextIntlClientProvider
          locale="en"
          messages={en as Record<string, unknown>}
          timeZone="UTC"
        >
          <PublicEndpointUsage resourceId="catalog" refreshKey={1} />
        </NextIntlClientProvider>,
      );
    });
    await waitFor(() => expect(mockGetStats).toHaveBeenCalledTimes(2));
  });
});

describe("shouldShowPublicEndpointUsage", () => {
  const live = { public_mcp_endpoint_live: true, resource_id: "catalog" };

  it("is true only for an owner of a context with a live endpoint", () => {
    expect(shouldShowPublicEndpointUsage(live, "owner")).toBe(true);
    expect(shouldShowPublicEndpointUsage(live, "admin")).toBe(false);
    expect(shouldShowPublicEndpointUsage(live, "member")).toBe(false);
    expect(shouldShowPublicEndpointUsage(live, "viewer")).toBe(false);
    expect(shouldShowPublicEndpointUsage(live, undefined)).toBe(false);
    expect(
      shouldShowPublicEndpointUsage(
        { ...live, public_mcp_endpoint_live: false },
        "owner",
      ),
    ).toBe(false);
    expect(
      shouldShowPublicEndpointUsage(
        { public_mcp_endpoint_live: true, resource_id: null },
        "owner",
      ),
    ).toBe(false);
    expect(
      shouldShowPublicEndpointUsage({ resource_id: "catalog" }, "owner"),
    ).toBe(false);
  });
});
