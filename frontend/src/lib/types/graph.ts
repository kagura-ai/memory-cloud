/**
 * Graph types for Neural Memory visualization
 * Issue #31 - Redesigned graph view
 */

export interface GraphNode {
  id: string;
  summary: string;
  type: string;
  importance: number;
  degree: number;
  // Backend exposes this as `str | None`; nullable in the wire shape, not
  // just absent. Treat absence and explicit null the same in renderers.
  created_at?: string | null;
}

export interface GraphEdge {
  source: string;
  target: string;
  weight: number;
  type: string;
  // #430: surfaced from the backend response so downstream UI consumers
  // (e.g. the graph edge metadata overlay #435) can render edge creation
  // time + LLM-judge confidence. Backend types are `str | None` /
  // `float | None`; allowing both null and absent here keeps the type
  // honest and saves call sites from `as unknown as` casts.
  created_at?: string | null;
  confidence?: number | null;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: {
    total_nodes: number;
    total_edges: number;
    filtered_nodes: number;
    filtered_edges: number;
  };
}

export interface GraphFilters {
  limit_nodes?: number;
  min_weight?: number;
  memory_types?: string[];
}

export interface GraphStatsResponse {
  user_id: string;
  stats: {
    total_nodes: number;
    total_edges: number;
    avg_edge_weight: number;
    max_edge_weight: number;
    min_edge_weight: number;
    density: number;
    top_connections: Array<{
      node_id: string;
      summary: string;
      degree: number;
      type?: string;
      edge_count?: number;
    }>;
    recent_edges: Array<{
      source: string;
      target: string;
      weight: number;
    }>;
  };
  last_updated: string;
}

// Memory type color mapping
export const MEMORY_TYPE_COLORS: Record<string, string> = {
  code: "#10b981",
  note: "#3b82f6",
  decision: "#8b5cf6",
  error: "#ef4444",
  feature: "#f59e0b",
  bug: "#ec4899",
  refactor: "#06b6d4",
  test: "#14b8a6",
  docs: "#6366f1",
  unknown: "#6b7280",
};

export function getMemoryTypeColor(type: string): string {
  return MEMORY_TYPE_COLORS[type.toLowerCase()] || MEMORY_TYPE_COLORS.unknown;
}
