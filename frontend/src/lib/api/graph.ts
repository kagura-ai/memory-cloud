/**
 * Graph API client for Neural Memory visualization
 * Issue #31 - Redesigned graph view
 */

import { apiClient } from "./base";
import type {
  GraphData,
  GraphFilters,
  GraphStatsResponse,
} from "../types/graph";

// #1416 mirror of backend `CreateEdgeRequest` (routes/graph.py). Creates a
// user-declared edge between two memories in a context. The supersede-suggestion
// confirm flow (#1403) posts with `edge_type: "supersedes"`.
export interface CreateEdgeRequest {
  context_id: string;
  source_id: string;
  target_id: string;
  /** Defaults to "related_to" server-side; must be a DB-accepted edge type. */
  edge_type?: string;
  weight?: number;
  confidence?: number;
  /** Re-assert an existing declared edge with different values (default false). */
  overwrite?: boolean;
}

// Mirror of backend `CreateEdgeResponse`. `operation` is created|updated|unchanged.
export interface CreateEdgeResponse {
  operation: string;
  edge: Record<string, unknown> | null;
  previous?: Record<string, unknown> | null;
}

export const graphApi = {
  async getGraphData(
    contextId: string,
    filters?: GraphFilters,
  ): Promise<GraphData> {
    const params = new URLSearchParams();
    params.append("context_id", contextId);

    if (filters?.limit_nodes) {
      params.append("limit_nodes", filters.limit_nodes.toString());
    }
    if (filters?.min_weight !== undefined) {
      params.append("min_weight", filters.min_weight.toString());
    }
    if (filters?.memory_types && filters.memory_types.length > 0) {
      filters.memory_types.forEach((type) => {
        params.append("memory_types", type);
      });
    }

    return apiClient.get<GraphData>(`/api/v1/graph/data?${params.toString()}`);
  },

  async getGraphStats(contextId: string): Promise<GraphStatsResponse> {
    return apiClient.get<GraphStatsResponse>(
      `/api/v1/graph/stats?context_id=${contextId}`,
    );
  },

  // #1416: create a user-declared edge (REST twin of the MCP `create_edge`
  // tool). Used by the supersede-suggestion confirm flow to POST a
  // `supersedes` edge, which self-heals the stored suggestion (#1403).
  async createEdge(request: CreateEdgeRequest): Promise<CreateEdgeResponse> {
    return apiClient.post<CreateEdgeResponse>("/api/v1/graph/edges", request);
  },
};
