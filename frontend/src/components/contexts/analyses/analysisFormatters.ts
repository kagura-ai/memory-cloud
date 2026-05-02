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
  /**
   * Per-type counts. Ratios are computed at render time from the
   * (possibly aggregated across clusters) counts so the per-cluster
   * vs all-clusters views can share the same denominator math —
   * carrying ratios at the per-cluster layer would size-weight
   * incorrectly when summing across clusters of different sizes.
   */
  typeDistribution: Array<{ type: string; count: number }>;
  importanceBuckets: number[];
  timeSeries: Array<{ bucket: string; count: number }>;
}

const EMPTY_STATS: NormalizedPropertyStats = {
  topTags: [],
  typeDistribution: [],
  importanceBuckets: [],
  timeSeries: [],
};

function isTagCountPair(v: unknown): v is { tag: string; count: number } {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Record<string, unknown>).tag === "string" &&
    typeof (v as Record<string, unknown>).count === "number"
  );
}

function isTimeBucketRow(
  v: unknown,
): v is { start: string; end?: string; count: number } {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as Record<string, unknown>).start === "string" &&
    typeof (v as Record<string, unknown>).count === "number"
  );
}

/**
 * Convert ``backend/src/services/analysis/property_stats.py``'s
 * persisted ``property_stats`` JSONB into the shape PropertyStats.tsx
 * renders. Backend writes:
 *
 *   { tags: [{tag,count}], types: {type→count},
 *     importance: [int,int,int,int], time: [{start,end,count}, ...] }
 *
 * The UI expects:
 *
 *   { topTags, typeDistribution: [{type,ratio}],
 *     importanceBuckets: number[], timeSeries: [{bucket,count}] }
 *
 * Type counts are converted to ratios on the frontend so the bar chart
 * always sums to 1.0 even if the persisted dict has a partial set of
 * types. Time buckets keep ``start`` as the rendered ``bucket`` label
 * (the SVG bar chart does not need the explicit ``end``).
 */
export function normalizePropertyStats(
  raw: Record<string, unknown> | null | undefined,
): NormalizedPropertyStats {
  if (!raw) return EMPTY_STATS;

  const tagsRaw = raw.tags;
  const topTags = Array.isArray(tagsRaw)
    ? tagsRaw.filter(isTagCountPair).slice(0, 8)
    : [];

  const typesRaw = raw.types;
  let typeDistribution: NormalizedPropertyStats["typeDistribution"] = [];
  if (
    typesRaw !== null &&
    typeof typesRaw === "object" &&
    !Array.isArray(typesRaw)
  ) {
    const entries = Object.entries(typesRaw as Record<string, unknown>).filter(
      ([, count]) => typeof count === "number" && (count as number) >= 0,
    ) as Array<[string, number]>;
    typeDistribution = entries
      .sort(([, a], [, b]) => b - a)
      .slice(0, 6)
      .map(([type, count]) => ({ type, count }));
  }

  const importanceRaw = raw.importance;
  const importanceBuckets =
    Array.isArray(importanceRaw) &&
    importanceRaw.every((n) => typeof n === "number")
      ? (importanceRaw as number[]).slice(0, 4)
      : [];

  const seriesRaw = raw.time;
  const timeSeries = Array.isArray(seriesRaw)
    ? seriesRaw
        .filter(isTimeBucketRow)
        .slice(0, 24)
        .map((row) => ({ bucket: row.start, count: row.count }))
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
