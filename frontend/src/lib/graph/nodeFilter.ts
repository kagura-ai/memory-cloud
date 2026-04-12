/**
 * Top-N node filter for the bounded neural graph visualization.
 *
 * Three selectable strategies, each with deterministic tie-breaking by node.id
 * so consecutive renders at the same (strategy, N) produce identical subsets.
 * The induced edge subgraph keeps only edges where both endpoints survived.
 *
 * Issue #233 — bounded neural graph visualization.
 */

import type { GraphNode, GraphEdge } from "@/lib/types/graph";

export type FilterStrategy = "degree" | "importance" | "weightSum";

interface FilterInput {
  nodes: GraphNode[];
  edges: GraphEdge[];
  n: number;
  strategy: FilterStrategy;
}

interface FilterOutput {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export function applyFilter({
  nodes,
  edges,
  n,
  strategy,
}: FilterInput): FilterOutput {
  const clampedN = Math.max(0, Math.min(n, nodes.length));
  if (clampedN === 0) return { nodes: [], edges: [] };

  const scores = new Map<string, number>();

  if (strategy === "degree") {
    for (const node of nodes) {
      scores.set(node.id, node.degree ?? 0);
    }
  } else if (strategy === "importance") {
    for (const node of nodes) {
      scores.set(node.id, node.importance ?? 0);
    }
  } else {
    // weightSum — sum of adjacent edge weights (pure edge-weight centrality)
    const weightSum = new Map<string, number>();
    for (const edge of edges) {
      weightSum.set(
        edge.source,
        (weightSum.get(edge.source) ?? 0) + edge.weight,
      );
      weightSum.set(
        edge.target,
        (weightSum.get(edge.target) ?? 0) + edge.weight,
      );
    }
    for (const node of nodes) {
      scores.set(node.id, weightSum.get(node.id) ?? 0);
    }
  }

  const sorted = [...nodes].sort((a, b) => {
    const sa = scores.get(a.id) ?? 0;
    const sb = scores.get(b.id) ?? 0;
    if (sb !== sa) return sb - sa;
    // Locale-independent tie-break for cross-environment determinism
    if (a.id < b.id) return -1;
    if (a.id > b.id) return 1;
    return 0;
  });

  const top = sorted.slice(0, clampedN);
  const topIds = new Set(top.map((node) => node.id));
  const filteredEdges = edges.filter(
    (edge) => topIds.has(edge.source) && topIds.has(edge.target),
  );

  return { nodes: top, edges: filteredEdges };
}
