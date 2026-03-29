/**
 * Graph API client for Neural Memory visualization
 * Issue #31 - Redesigned graph view
 */

import { apiClient } from "./base";
import type { GraphData, GraphFilters, GraphStatsResponse } from "../types/graph";

export const graphApi = {
  async getGraphData(contextId: string, filters?: GraphFilters): Promise<GraphData> {
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
    return apiClient.get<GraphStatsResponse>(`/api/v1/graph/stats?context_id=${contextId}`);
  },
};
