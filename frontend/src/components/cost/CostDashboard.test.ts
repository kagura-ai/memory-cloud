/**
 * Tests for the pure helpers in CostDashboard.tsx (Issue #473).
 *
 * Scope is the data-shaping logic — sticky-NULL aggregation in
 * ``buildChartData`` and null-as-em-dash in ``formatCost``. Component
 * rendering is exercised end-to-end by the Next.js prerender during
 * ``next build``; the focus here is the contracts that would silently
 * regress if someone edited the helpers.
 */

import { describe, expect, it } from "vitest";

import type { CostAggregationRow } from "@/lib/api";
import { buildChartData, formatCost } from "./CostDashboard";

function row(overrides: Partial<CostAggregationRow>): CostAggregationRow {
  return {
    period_start: "2026-04-01",
    workspace_id: "11111111-1111-1111-1111-111111111111",
    user_id: "user-1",
    calls: 0,
    tokens_in: 0,
    tokens_out: 0,
    tokens_cached_in: 0,
    embedding_tokens: 0,
    cost_usd: 0,
    cost_usd_byok: 0,
    cost_breakdown_by_model: [],
    cost_breakdown_by_source: [],
    ...overrides,
  };
}

describe("formatCost", () => {
  it("renders a positive number as $X.XXXX", () => {
    expect(formatCost(0.083)).toBe("$0.0830");
    expect(formatCost(125.5)).toBe("$125.5000");
  });

  it("renders zero as $0.0000 — distinct from cost-unknown", () => {
    // Genuine $0 must NOT collapse to em-dash; otherwise the UI would
    // conflate "no spend" with "cost unknown" and break the v1
    // cost-grade contract.
    expect(formatCost(0)).toBe("$0.0000");
  });

  it("renders null as em-dash (cost unknown)", () => {
    // Sticky-NULL contract from the backend: null means "at least one
    // contributing usage row had no resolved pricing." Render it as
    // "—" so operators don't read it as $0.
    expect(formatCost(null)).toBe("—");
  });
});

describe("buildChartData", () => {
  it("returns an empty array when no rows are provided", () => {
    expect(buildChartData([])).toEqual([]);
  });

  it("collapses multiple rows on the same date into one bucket", () => {
    const result = buildChartData([
      row({ period_start: "2026-04-01", cost_usd: 1.0, cost_usd_byok: 0.5 }),
      row({ period_start: "2026-04-01", cost_usd: 2.0, cost_usd_byok: 0.25 }),
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].date).toBe("2026-04-01");
    expect(result[0].cost_usd).toBeCloseTo(3.0, 6);
    expect(result[0].cost_usd_byok).toBeCloseTo(0.75, 6);
  });

  it("sorts buckets by date ascending (lexicographic on YYYY-MM-DD)", () => {
    const result = buildChartData([
      row({ period_start: "2026-04-30", cost_usd: 1.0 }),
      row({ period_start: "2026-04-01", cost_usd: 2.0 }),
      row({ period_start: "2026-04-15", cost_usd: 3.0 }),
    ]);
    expect(result.map((p) => p.date)).toEqual([
      "2026-04-01",
      "2026-04-15",
      "2026-04-30",
    ]);
  });

  it("propagates null cost via sticky-NULL: any null contribution → bucket is null", () => {
    // The chart MUST render a gap for unpriced periods. Summing
    // null + 1.5 = 1.5 would silently understate "cost unknown" as a
    // misleading $1.50 dip — the exact bug the backend's BOOL_AND CTE
    // exists to prevent.
    const result = buildChartData([
      row({ period_start: "2026-04-01", cost_usd: 1.5, cost_usd_byok: 0.0 }),
      row({ period_start: "2026-04-01", cost_usd: null, cost_usd_byok: 0.0 }),
    ]);
    expect(result).toEqual([
      { date: "2026-04-01", cost_usd: null, cost_usd_byok: 0.0 },
    ]);
  });

  it("sticky-NULL is per-field — cost_usd_byok null does NOT taint cost_usd", () => {
    // Independent buckets per field: a workspace running BYOK with
    // unpriced models shouldn't blank out its platform-billed total.
    const result = buildChartData([
      row({
        period_start: "2026-04-01",
        cost_usd: 1.0,
        cost_usd_byok: null,
      }),
      row({
        period_start: "2026-04-01",
        cost_usd: 2.0,
        cost_usd_byok: 0.5,
      }),
    ]);
    expect(result).toEqual([
      { date: "2026-04-01", cost_usd: 3.0, cost_usd_byok: null },
    ]);
  });

  it("once a bucket goes null it stays null even if a later non-null row arrives", () => {
    // Reduce-style: the order of the input rows is irrelevant — a
    // single null contribution permanently sinks the bucket.
    const result = buildChartData([
      row({ period_start: "2026-04-01", cost_usd: null }),
      row({ period_start: "2026-04-01", cost_usd: 5.0 }),
      row({ period_start: "2026-04-01", cost_usd: 10.0 }),
    ]);
    expect(result[0].cost_usd).toBeNull();
  });

  it("keeps separate buckets independent — null on one date does not leak to another", () => {
    const result = buildChartData([
      row({ period_start: "2026-04-01", cost_usd: null }),
      row({ period_start: "2026-04-02", cost_usd: 3.5 }),
    ]);
    const byDate = Object.fromEntries(result.map((p) => [p.date, p.cost_usd]));
    expect(byDate["2026-04-01"]).toBeNull();
    expect(byDate["2026-04-02"]).toBe(3.5);
  });
});
