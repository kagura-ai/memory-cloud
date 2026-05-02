/**
 * Deterministic cluster color palette (Issue #497).
 *
 * Maps a 0-based ``cluster_index`` to a tailwind ``chart-N`` CSS
 * variable. The first 12 clusters get distinct hues from the palette
 * extended in ``app/globals.css`` (chart-1 .. chart-12); higher
 * indices wrap modulo 12 — KMeans n_clusters in v1 is bounded by
 * ``ceil(sqrt(memory_count))`` ≈ 90, but the visual contrast budget
 * runs out well before that and re-using hues is honest about the
 * eye-distinction limit rather than synthesizing arbitrary near-
 * identical colors at index 13+.
 *
 * Returned colors are CSS ``hsl(var(--chart-N))`` strings so dark
 * mode automatically applies the corresponding dark palette without
 * a JS-side theme observer.
 */

export const CLUSTER_PALETTE_SIZE = 12;

/**
 * Resolve a cluster index to its CSS color string.
 *
 * @example
 *   <circle fill={getClusterColor(3)} />
 *   <span style={{ backgroundColor: getClusterColor(cluster.cluster_index) }} />
 */
export function getClusterColor(clusterIndex: number): string {
  const slot = (clusterIndex % CLUSTER_PALETTE_SIZE) + 1;
  return `hsl(var(--chart-${slot}))`;
}
