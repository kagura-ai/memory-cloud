/**
 * Graph types for Neural Memory visualization
 * Issue #60 - Neural Memory Graph Visualization
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

export interface GraphStats {
  total_nodes: number;
  total_edges: number;
  filtered_nodes: number;
  filtered_edges: number;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: GraphStats;
}

export interface GraphFilters {
  limit_nodes?: number;
  min_weight?: number;
  memory_types?: string[];
}

// Memory type color mapping for consistent visualization
export const MEMORY_TYPE_COLORS: Record<string, string> = {
  code: "#10b981", // green
  note: "#3b82f6", // blue
  decision: "#8b5cf6", // purple
  error: "#ef4444", // red
  feature: "#f59e0b", // amber
  bug: "#ec4899", // pink
  refactor: "#06b6d4", // cyan
  test: "#14b8a6", // teal
  docs: "#6366f1", // indigo
  unknown: "#6b7280", // gray
};

// Get color for memory type
export function getMemoryTypeColor(type: string): string {
  return MEMORY_TYPE_COLORS[type.toLowerCase()] || MEMORY_TYPE_COLORS.unknown;
}

// Available graph layouts
export type GraphLayout = "force" | "dagre" | "circular";

export interface LayoutOptions {
  layout: GraphLayout;
  direction?: "TB" | "LR" | "BT" | "RL"; // For dagre layout
}
