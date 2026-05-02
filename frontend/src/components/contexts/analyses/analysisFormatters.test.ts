/**
 * Pure-helper unit tests for the analyses formatters.
 *
 * Following the PR #527 cost-dashboard convention: no component
 * render here — only data transforms.
 */

import { describe, it, expect } from "vitest";
import {
  formatCostCents,
  formatConfidence,
  classifyQuality,
  normalizePropertyStats,
  normalize01,
  computePositionBox,
} from "./analysisFormatters";

describe("formatCostCents", () => {
  it("renders dollars with three decimals", () => {
    expect(formatCostCents(1100)).toBe("$11.000");
    expect(formatCostCents(11)).toBe("$0.110");
    expect(formatCostCents(0)).toBe("$0.000");
  });

  it("renders sticky-NULL as em-dash", () => {
    // Critical: a null cost must NOT render as $0.000 (would mislead
    // operator into thinking the run was free vs unpriced).
    expect(formatCostCents(null)).toBe("—");
    expect(formatCostCents(undefined)).toBe("—");
  });
});

describe("formatConfidence", () => {
  it("renders confidence to 2 decimals", () => {
    expect(formatConfidence(0.91)).toBe("0.91");
    expect(formatConfidence(0.5)).toBe("0.50");
    expect(formatConfidence(1)).toBe("1.00");
  });

  it("returns em-dash for null", () => {
    expect(formatConfidence(null)).toBe("—");
    expect(formatConfidence(undefined)).toBe("—");
  });
});

describe("classifyQuality", () => {
  it("maps 0.85+ to good", () => {
    expect(classifyQuality(0.85)).toBe("good");
    expect(classifyQuality(0.91)).toBe("good");
    expect(classifyQuality(1)).toBe("good");
  });

  it("maps 0.7..0.85 to fair", () => {
    expect(classifyQuality(0.7)).toBe("fair");
    expect(classifyQuality(0.78)).toBe("fair");
    expect(classifyQuality(0.849)).toBe("fair");
  });

  it("maps below 0.7 to poor", () => {
    expect(classifyQuality(0.69)).toBe("poor");
    expect(classifyQuality(0.4)).toBe("poor");
  });

  it("returns null for missing input", () => {
    expect(classifyQuality(null)).toBeNull();
    expect(classifyQuality(undefined)).toBeNull();
  });
});

describe("normalizePropertyStats", () => {
  it("returns empty shape for null/undefined", () => {
    const out = normalizePropertyStats(null);
    expect(out.topTags).toEqual([]);
    expect(out.typeDistribution).toEqual([]);
    expect(out.importanceBuckets).toEqual([]);
    expect(out.timeSeries).toEqual([]);
  });

  it("normalizes backend-shape payload (tags / types / importance / time)", () => {
    // Mirror the actual ``property_stats`` JSONB written by
    // ``backend/src/services/analysis/property_stats.py:aggregate_cluster_stats``.
    const out = normalizePropertyStats({
      tags: [
        { tag: "kouchou-ai", count: 38 },
        { tag: "design", count: 12 },
      ],
      types: { "feature-design": 71, decision: 18, pattern: 11 },
      importance: [3, 5, 8, 7],
      time: [
        {
          start: "2026-04-21T00:00:00Z",
          end: "2026-04-22T00:00:00Z",
          count: 4,
        },
        {
          start: "2026-04-22T00:00:00Z",
          end: "2026-04-23T00:00:00Z",
          count: 8,
        },
      ],
    });
    expect(out.topTags).toEqual([
      { tag: "kouchou-ai", count: 38 },
      { tag: "design", count: 12 },
    ]);
    // dict-of-counts → list-of-ratios sorted by count desc, normalized so sum=1
    expect(out.typeDistribution.map((r) => r.type)).toEqual([
      "feature-design",
      "decision",
      "pattern",
    ]);
    expect(out.typeDistribution[0].ratio).toBeCloseTo(71 / 100, 6);
    expect(out.typeDistribution[1].ratio).toBeCloseTo(18 / 100, 6);
    expect(out.importanceBuckets).toEqual([3, 5, 8, 7]);
    // time bucket: keep ``start`` as the rendered label for the bar chart
    expect(out.timeSeries).toEqual([
      { bucket: "2026-04-21T00:00:00Z", count: 4 },
      { bucket: "2026-04-22T00:00:00Z", count: 8 },
    ]);
  });

  it("filters non-conforming entries from each list", () => {
    // Backend stores property_stats as JSONB so the runtime shape can
    // drift if the labeler emits a malformed entry. The helper must
    // tolerate this by dropping bad rows rather than throwing.
    const out = normalizePropertyStats({
      tags: [
        { tag: "kouchou-ai", count: 38 },
        { tag: "missing-count" }, // dropped — no count
        "string-not-object", // dropped
        { tag: "design", count: 12 },
      ],
      types: { decision: "not-a-number", pattern: 5 }, // string value silently dropped
      importance: [0.3, 0.55, 0.85, 0.7, "ignored"], // mixed type → all-or-nothing
      time: [
        { start: "2026-04-21T00:00:00Z", count: 4 }, // ``end`` is optional
        {
          start: "2026-04-22T00:00:00Z",
          end: "2026-04-23T00:00:00Z",
          count: 8,
        },
      ],
    });
    expect(out.topTags).toEqual([
      { tag: "kouchou-ai", count: 38 },
      { tag: "design", count: 12 },
    ]);
    // Only ``pattern`` remained valid → 100% of the distribution.
    expect(out.typeDistribution).toEqual([{ type: "pattern", ratio: 1 }]);
    expect(out.importanceBuckets).toEqual([]);
    expect(out.timeSeries).toHaveLength(2);
  });

  it("caps the lists to bounded sizes", () => {
    // Defense against a runaway labeler emitting hundreds of tags.
    const types: Record<string, number> = {};
    for (let i = 0; i < 50; i++) types[`tp${i}`] = i;
    const out = normalizePropertyStats({
      tags: Array.from({ length: 50 }, (_, i) => ({
        tag: `t${i}`,
        count: i,
      })),
      types,
      time: Array.from({ length: 100 }, (_, i) => ({
        start: `2026-04-${String(i + 1).padStart(2, "0")}T00:00:00Z`,
        count: i,
      })),
    });
    expect(out.topTags).toHaveLength(8);
    expect(out.typeDistribution).toHaveLength(6);
    expect(out.timeSeries).toHaveLength(24);
  });
});

describe("normalize01", () => {
  it("maps value to [0,1] within range", () => {
    expect(normalize01(0, 0, 10)).toBeCloseTo(0, 6);
    expect(normalize01(5, 0, 10)).toBeCloseTo(0.5, 6);
    expect(normalize01(10, 0, 10)).toBeCloseTo(1, 6);
  });

  it("clamps out-of-range to [0,1]", () => {
    expect(normalize01(-5, 0, 10)).toBe(0);
    expect(normalize01(15, 0, 10)).toBe(1);
  });

  it("returns 0.5 on degenerate range", () => {
    // SVG layout would otherwise divide by zero; centering is the
    // safest visual fallback for a single-cluster run.
    expect(normalize01(5, 5, 5)).toBe(0.5);
    expect(normalize01(NaN, 0, 10)).toBe(0.5);
  });
});

describe("computePositionBox", () => {
  it("computes bounding box in one pass", () => {
    const box = computePositionBox([
      { x: 1, y: -2 },
      { x: 3, y: 5 },
      { x: -1, y: 0 },
    ]);
    expect(box.minX).toBe(-1);
    expect(box.maxX).toBe(3);
    expect(box.minY).toBe(-2);
    expect(box.maxY).toBe(5);
  });

  it("returns degenerate unit box on empty input", () => {
    // Avoids returning Infinity so normalize01 callers always see a
    // finite range without an extra guard.
    const box = computePositionBox([]);
    expect(box.minX).toBe(0);
    expect(box.maxX).toBe(1);
    expect(box.minY).toBe(0);
    expect(box.maxY).toBe(1);
  });
});
