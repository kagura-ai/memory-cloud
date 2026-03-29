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
  created_at?: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  weight: number;
  type: string;
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
