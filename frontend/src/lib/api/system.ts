/**
 * System Management API Client
 *
 * Issue #684 - System Backend Status and Vector Collections
 */

import { apiClient } from "./base";

// ============================================================================
// Types
// ============================================================================

export interface BackendInfo {
  type: string;
  url: string | null;
  connected: boolean;
  status: "ok" | "warning" | "error" | "info";
  message: string;
  stats: Record<string, any> | null;
}

export interface BackendStatusResponse {
  database: BackendInfo;
  vector_db: BackendInfo;
  cache: BackendInfo;
}

export interface VectorCollectionInfo {
  name: string;
  vector_count: number;
  embedding_dimension: number | null;
}

export interface VectorCollectionsResponse {
  backend: string;
  collections: VectorCollectionInfo[];
}

export interface HealthResponse {
  status: "healthy" | "unhealthy" | "degraded";
  version: string;
  uptime_seconds: number;
  timestamp: string;
}

// ============================================================================
// System APIs
// ============================================================================

/**
 * Get backend status (Database, Vector DB, Cache)
 */
export async function getBackendStatus(): Promise<BackendStatusResponse> {
  return await apiClient.get<BackendStatusResponse>("/system/backends");
}

/**
 * Get vector database collections
 */
export async function getVectorCollections(): Promise<VectorCollectionsResponse> {
  return await apiClient.get<VectorCollectionsResponse>(
    "/system/vector/collections",
  );
}

/**
 * Restart the application
 * Note: This endpoint is admin-only and requires application restart permissions
 */
export async function restartApplication(): Promise<void> {
  await apiClient.post("/system/restart");
}

/**
 * Poll health endpoint until application is back online
 * @param maxAttempts - Maximum number of polling attempts (default: 60)
 * @param intervalMs - Interval between attempts in milliseconds (default: 1000)
 * @returns true if application is healthy, false if max attempts reached
 */
export async function pollHealth(
  maxAttempts: number = 60,
  intervalMs: number = 1000,
): Promise<boolean> {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const health = await apiClient.get<HealthResponse>("/health");
      if (health.status === "healthy") {
        return true;
      }
    } catch (error) {
      // Ignore errors during polling (application may still be restarting)
    }

    // Wait before next attempt
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }

  return false;
}

/**
 * Get application health status
 */
export async function getHealth(): Promise<HealthResponse> {
  return await apiClient.get<HealthResponse>("/health");
}

export interface SystemInfo {
  name: string;
  version: string;
  description: string;
  environment: string;
  /** Backend feature flags (e.g. `plan_page`, `neural_memory`). */
  features: Record<string, boolean>;
}

/**
 * Get system info — version + backend feature flags (Issue #1145).
 * Used to gate UI surfaces (e.g. the Plan page) behind a backend toggle.
 */
export async function getSystemInfo(): Promise<SystemInfo> {
  return await apiClient.get<SystemInfo>("/api/v1/system/info");
}

// ============================================================================
// Issue #31: New APIs for System Overview
// ============================================================================

export interface SystemOverview {
  database: {
    status: string;
    type: string;
    version: string;
    connections: {
      active: number;
      idle: number;
      max: number;
    };
    size: string;
    tables: number;
  };
  qdrant: {
    status: string;
    version: string;
    collections: number;
    vectors: number;
    memory_usage: string;
  };
  redis: {
    status: string;
    version: string;
    memory_usage: string;
    keys: number;
    uptime_seconds: number;
  };
  graph: {
    status: string;
    nodes: number;
    edges: number;
    memory_usage: string;
  };
  neural: {
    status: string;
    learning_rate: number;
    consolidation_enabled: boolean;
    last_consolidation: string;
    total_associations: number;
  };
  overall_health: string;
  uptime_seconds: number;
  version: string;
}

export interface MCPStatus {
  running: boolean;
  sessions: number;
  uptime_seconds: number;
  tools: Array<{
    name: string;
    description: string;
    usage_count: number;
  }>;
  version: string;
}

/**
 * Get comprehensive system overview (DB, VDB, Cache, Graph, Neural)
 * Issue #45 - Real API implementation
 */
export async function getSystemOverview(): Promise<SystemOverview> {
  return await apiClient.get<SystemOverview>("/api/v1/system/overview");
}

/**
 * Get MCP server status
 * Issue #45 - Real API implementation
 */
export async function getMCPStatus(): Promise<MCPStatus> {
  return await apiClient.get<MCPStatus>("/api/v1/mcp/status");
}
