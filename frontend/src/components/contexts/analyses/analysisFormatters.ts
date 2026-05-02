/**
 * Pure formatting helpers for the analyses tab (Issue #497).
 *
 * Following the PR #527 cost-dashboard convention: extract pure data
 * transforms here, unit-test them in ``analysisFormatters.test.ts``,
 * leave the React components as thin presentational layers.
 */

/**
 * Render an integer-cents cost as a USD-prefixed dollar string with
 * 3 decimal places. Sticky-NULL: a ``null`` value renders as ``"—"``
 * (em-dash) so an unpriced run does NOT visually equal a $0.000 run.
 *
 * Mirrors ``formatCost`` from ``components/cost/CostDashboard.tsx``
 * but accepts integer cents (the analyses API surface) rather than
 * USD floats.
 *
 * @example
 *   formatCostCents(1100) === "$11.000"
 *   formatCostCents(11)   === "$0.110"
 *   formatCostCents(0)    === "$0.000"
 *   formatCostCents(null) === "—"
 */
export function formatCostCents(value: number | null | undefined): string {
  if (value == null) return "—";
  return `$${(value / 100).toFixed(3)}`;
}

/**
 * Render a confidence/quality score (0..1) as a 2-decimal string.
 * Sticky-NULL: ``null`` becomes ``"—"`` to match the cost rendering.
 */
export function formatConfidence(value: number | null | undefined): string {
  if (value == null) return "—";
  return value.toFixed(2);
}

/**
 * Bin a label-confidence score into a quality badge token.
 *
 * The 0.85 / 0.7 thresholds are picked to match the prototype's three-
 * tier visual ("good" / "fair" / "poor") and the labeler's typical
 * output distribution on Gemini Flash Lite (most legitimate clusters
 * land at 0.78+; outlier / weak clusters fall below 0.7).
 */
export type QualityTier = "good" | "fair" | "poor";

export function classifyQuality(
  confidence: number | null | undefined,
): QualityTier | null {
  if (confidence == null) return null;
  if (confidence >= 0.85) return "good";
  if (confidence >= 0.7) return "fair";
  return "poor";
}

/**
 * Coerce arbitrary ``property_stats`` payloads into a typed shape the
 * UI can render without ``any``. Backend stores property_stats as
 * ``JSONB`` so the wire type is ``Record<string, unknown>``; this
 * helper does the runtime narrowing in one place.
 */
export interface NormalizedPropertyStats {
  topTags: Array<{ tag: string; count: number }>;
  typeDistribution: Array<{ type: string; ratio: number }>;
  importanceBuckets: number[];
  timeSeries: Array<{ bucket: string; count: number }>;
}

const EMPTY_STATS: NormalizedPropertyStats = {
  topTags: [],
  typeDistribution: [],
  importanceBuckets: [],
  timeSeries: [],
};

function isStringNumberPair(v: unknown): v is { tag: string; count: number } {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Record<string, unknown>).tag === "string" &&
    typeof (v as Record<string, unknown>).count === "number"
  );
}

function isTypeRatioPair(v: unknown): v is { type: string; ratio: number } {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Record<string, unknown>).type === "string" &&
    typeof (v as Record<string, unknown>).ratio === "number"
  );
}

function isTimeSeriesPair(v: unknown): v is { bucket: string; count: number } {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Record<string, unknown>).bucket === "string" &&
    typeof (v as Record<string, unknown>).count === "number"
  );
}

export function normalizePropertyStats(
  raw: Record<string, unknown> | null | undefined,
): NormalizedPropertyStats {
  if (!raw) return EMPTY_STATS;

  const topTagsRaw = raw.top_tags;
  const topTags = Array.isArray(topTagsRaw)
    ? topTagsRaw.filter(isStringNumberPair).slice(0, 8)
    : [];

  const typesRaw = raw.type_distribution;
  const typeDistribution = Array.isArray(typesRaw)
    ? typesRaw.filter(isTypeRatioPair).slice(0, 6)
    : [];

  const importanceRaw = raw.importance_buckets;
  const importanceBuckets =
    Array.isArray(importanceRaw) &&
    importanceRaw.every((n) => typeof n === "number")
      ? (importanceRaw as number[]).slice(0, 4)
      : [];

  const seriesRaw = raw.time_series;
  const timeSeries = Array.isArray(seriesRaw)
    ? seriesRaw.filter(isTimeSeriesPair).slice(0, 24)
    : [];

  return { topTags, typeDistribution, importanceBuckets, timeSeries };
}

/**
 * Project an arbitrary numeric range to ``[0, 1]`` for SVG layout.
 * Returns 0.5 when the range is degenerate (min === max) so the dot
 * lands at the center rather than NaN or +Infinity.
 *
 * Used by ``ScatterPlot`` to map UMAP coordinates to viewport ratios.
 */
export function normalize01(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return 0.5;
  if (max <= min) return 0.5;
  const t = (value - min) / (max - min);
  if (t < 0) return 0;
  if (t > 1) return 1;
  return t;
}

/**
 * Compute the bounding box of a position list in a single pass.
 *
 * Returns a degenerate ``{0, 0, 1, 1}`` box when the input is empty
 * so ``normalize01`` callers always have a finite range to work with.
 */
export interface PositionBox {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

export function computePositionBox(
  positions: Array<{ x: number; y: number }>,
): PositionBox {
  if (positions.length === 0) {
    return { minX: 0, maxX: 1, minY: 0, maxY: 1 };
  }
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const p of positions) {
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }
  return { minX, maxX, minY, maxY };
}
