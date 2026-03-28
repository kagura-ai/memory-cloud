/**
 * Graph API client for Neural Memory visualization
 * Issue #60 - Neural Memory Graph Visualization
 */

import { apiClient } from "./base";
import type { GraphData, GraphFilters } from "../types/graph";

export const graphApi = {
  /**
   * Get graph data for visualization with optional filtering
   */
  async getGraphData(filters?: GraphFilters): Promise<GraphData> {
    const params = new URLSearchParams();

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

    const query = params.toString();
    const url = `/api/v1/graph/data${query ? `?${query}` : ""}`;

    return apiClient.get<GraphData>(url);
  },

  /**
   * Get graph statistics
   */
  async getGraphStats() {
    return apiClient.get("/api/v1/graph/stats");
  },
};
