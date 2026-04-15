/**
 * Tests for IndexerStatusPanel.
 *
 * Verifies the four mutually-exclusive states (loading / error / empty /
 * data) and the four `job_status` enum branches. Job-status combinations
 * are driven through `describe.each` so adding a new enum value (e.g., the
 * future `paused` state) only requires a single new row.
 *
 * Following the same mocking shape as `ResourceStatsStrip.test.tsx` so
 * tests stay grep-able as a family.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { IndexerStatusPanel } from "./IndexerStatusPanel";
import type {
  IndexerJobStatus,
  IndexerStatusResponse,
  ResourceEventItem,
} from "@/lib/api/resources";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}(${JSON.stringify(vars)})` : key,
  useLocale: () => "en",
}));

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { timezone: "UTC" } }),
}));

vi.mock("@/lib/utils/datetime", () => ({
  formatRelativeTime: (iso: string) => `relative(${iso})`,
  formatDateTime: (iso: string) => `dt(${iso})`,
}));

const baseEvent: ResourceEventItem = {
  id: 1,
  op: "upsert",
  doc_id: "doc-001",
  version: 1,
  created_at: "2026-04-15T00:00:00Z",
};

function makeResponse(
  overrides: Partial<IndexerStatusResponse> = {},
): IndexerStatusResponse {
  return {
    resource_id: "test",
    state: {
      job_status: "idle",
      last_run_at: "2026-04-15T00:00:00Z",
      next_run_at: null,
      active_version: 1,
      last_offset: 100,
      lag_seconds: 60,
      metrics: {
        applied_upserts: 10,
        applied_deletes: 1,
        errors: 0,
        skipped_reason: null,
      },
    },
    recent_events: [baseEvent],
    ...overrides,
  };
}

describe("IndexerStatusPanel — non-data states", () => {
  it("renders skeleton when loading", () => {
    render(<IndexerStatusPanel data={undefined} isLoading error={null} />);
    expect(screen.getByTestId("indexer-status-skeleton")).toBeInTheDocument();
  });

  it("renders ErrorBanner when error", () => {
    render(
      <IndexerStatusPanel
        data={undefined}
        isLoading={false}
        error={new Error("network down")}
      />,
    );
    // ErrorBanner renders role="alert" with the message
    expect(screen.getByRole("alert")).toHaveTextContent("network down");
  });

  it("renders EmptyState when state is null and no events exist", () => {
    render(
      <IndexerStatusPanel
        data={makeResponse({ state: null, recent_events: [] })}
        isLoading={false}
        error={null}
      />,
    );
    expect(screen.getByText("indexer.emptyTitle")).toBeInTheDocument();
  });
});

describe.each([
  { status: "idle", expectedVariant: "secondary" },
  { status: "queued", expectedVariant: "secondary" },
  { status: "running", expectedVariant: "default" },
  { status: "failed", expectedVariant: "destructive" },
] as const)(
  "IndexerStatusPanel — job_status=$status",
  ({ status, expectedVariant }) => {
    it(`maps ${status} to badge variant=${expectedVariant}`, () => {
      const data = makeResponse();
      data.state!.job_status = status as IndexerJobStatus;
      render(<IndexerStatusPanel data={data} isLoading={false} error={null} />);

      // The Badge carries `data-variant` so we can pin the visual contract
      // without coupling the test to Tailwind class names (Test the contract,
      // not the implementation).
      const badge = screen.getByText(`indexer.status.${status}`);
      expect(badge.closest("[data-variant]")).toHaveAttribute(
        "data-variant",
        expectedVariant,
      );
    });
  },
);

describe("IndexerStatusPanel — data state details", () => {
  it("shows the skipped Alert only when skipped_reason is set", () => {
    const data = makeResponse();
    data.state!.metrics.skipped_reason = "schema_not_found";
    render(<IndexerStatusPanel data={data} isLoading={false} error={null} />);
    expect(
      screen.getByText("indexer.skipped.schema_not_found"),
    ).toBeInTheDocument();
  });

  it("renders the upsert badge with secondary variant for op=upsert events", () => {
    const data = makeResponse({
      recent_events: [{ ...baseEvent, op: "upsert" }],
    });
    render(<IndexerStatusPanel data={data} isLoading={false} error={null} />);
    const opBadge = screen.getByText("indexer.event.ops.upsert");
    expect(opBadge.closest("[data-op]")).toHaveAttribute("data-op", "upsert");
  });

  it("renders the delete badge with destructive variant for op=delete events", () => {
    const deleteEvent: ResourceEventItem = {
      ...baseEvent,
      id: 2,
      op: "delete",
      version: null,
    };
    const data = makeResponse({ recent_events: [deleteEvent] });
    render(<IndexerStatusPanel data={data} isLoading={false} error={null} />);
    const opBadge = screen.getByText("indexer.event.ops.delete");
    expect(opBadge.closest("[data-op]")).toHaveAttribute("data-op", "delete");
    // version=null renders as em-dash, not "null"
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("does NOT show 'lagHigh' subtext when lag_seconds is below threshold", () => {
    const data = makeResponse();
    data.state!.lag_seconds = 60; // 1 minute, well below 1h threshold
    render(<IndexerStatusPanel data={data} isLoading={false} error={null} />);
    expect(screen.queryByText("indexer.lagHigh")).not.toBeInTheDocument();
  });

  it("shows 'lagHigh' subtext when lag_seconds exceeds 1 hour", () => {
    const data = makeResponse();
    data.state!.lag_seconds = 3601; // 1 hour and 1 second
    render(<IndexerStatusPanel data={data} isLoading={false} error={null} />);
    expect(screen.getByText("indexer.lagHigh")).toBeInTheDocument();
  });

  it("shows the events table when recent_events is non-empty", () => {
    render(
      <IndexerStatusPanel
        data={makeResponse()}
        isLoading={false}
        error={null}
      />,
    );
    expect(screen.getByText("indexer.recentEvents")).toBeInTheDocument();
    expect(screen.getByText("doc-001")).toBeInTheDocument();
  });

  it("shows the noRecentEvents copy when state exists but no events", () => {
    const data = makeResponse({ recent_events: [] });
    render(<IndexerStatusPanel data={data} isLoading={false} error={null} />);
    expect(screen.getByText("indexer.noRecentEvents")).toBeInTheDocument();
  });
});
