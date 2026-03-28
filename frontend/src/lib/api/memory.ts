/**
 * Memory API Client
 *
 * Functions for interacting with Kagura Memory Cloud API
 */

import { apiClient } from './base';
import type {
  Memory,
  CreateMemoryRequest,
  UpdateMemoryRequest,
  MemorySearchParams,
  MemoryListResponse,
  MemoryStatsResponse,
} from '../types/memory';

/**
 * Get list of memories with optional filters
 */
export async function getMemories(
  params: MemorySearchParams = {}
): Promise<MemoryListResponse> {
  const searchParams = new URLSearchParams();

  if (params.query) searchParams.append('query', params.query);
  if (params.scope) searchParams.append('scope', params.scope);
  if (params.agent_name) searchParams.append('agent_name', params.agent_name);
  if (params.tags) params.tags.forEach(tag => searchParams.append('tags', tag));
  if (params.min_importance !== undefined) searchParams.append('min_importance', params.min_importance.toString());
  if (params.max_importance !== undefined) searchParams.append('max_importance', params.max_importance.toString());
  if (params.limit) searchParams.append('limit', params.limit.toString());
  if (params.offset) searchParams.append('offset', params.offset.toString());

  const queryString = searchParams.toString();
  const endpoint = `/memory${queryString ? `?${queryString}` : ''}`;

  return apiClient.get<MemoryListResponse>(endpoint);
}

/**
 * Get a single memory by key
 */
export async function getMemory(
  key: string,
  scope: string = 'persistent',
  agentName: string = 'global'
): Promise<Memory> {
  const searchParams = new URLSearchParams({
    scope,
    agent_name: agentName,
  });

  return apiClient.get<Memory>(`/memory/${encodeURIComponent(key)}?${searchParams.toString()}`);
}

/**
 * Create a new memory
 */
export async function createMemory(
  userId: string,
  data: CreateMemoryRequest
): Promise<Memory> {
  return apiClient.post<Memory>('/memory', {
    user_id: userId,
    ...data,
  });
}

/**
 * Update an existing memory
 */
export async function updateMemory(
  key: string,
  userId: string,
  data: UpdateMemoryRequest,
  scope: string = 'persistent',
  agentName: string = 'global'
): Promise<Memory> {
  return apiClient.put<Memory>(`/memory/${encodeURIComponent(key)}`, {
    user_id: userId,
    scope,
    agent_name: agentName,
    ...data,
  });
}

/**
 * Delete a memory
 */
export async function deleteMemory(
  key: string,
  userId: string,
  scope: string = 'persistent',
  agentName: string = 'global'
): Promise<void> {
  const searchParams = new URLSearchParams({
    user_id: userId,
    scope,
    agent_name: agentName,
  });

  return apiClient.delete<void>(`/memory/${encodeURIComponent(key)}?${searchParams.toString()}`);
}

/**
 * Get memory statistics
 */
export async function getMemoryStats(userId: string): Promise<MemoryStatsResponse> {
  return apiClient.get<MemoryStatsResponse>(`/memory/stats?user_id=${userId}`);
}

/**
 * Search memories semantically
 */
export async function searchMemories(
  userId: string,
  query: string,
  params: Omit<MemorySearchParams, 'query'> = {}
): Promise<MemoryListResponse> {
  return getMemories({
    ...params,
    query,
  });
}

/**
 * Bulk delete memories (Issue #666)
 */
export async function bulkDeleteMemories(
  keys: string[],
  scope: 'working' | 'persistent' = 'persistent',
  agentName: string = 'global'
): Promise<{ deleted_count: number; failed_keys: string[]; errors: Record<string, string> }> {
  return apiClient.post('/memory/bulk-delete', {
    keys,
    scope,
    agent_name: agentName,
  });
}


// ============================================================================
// Issue #720: New MCP Tools Integration - Search Functions
// ============================================================================

export interface SemanticSearchParams {
  query: string;
  k?: number;
  agent_name?: string;
}

export interface KeywordSearchParams {
  query: string;
  k?: number;
  agent_name?: string;
}

export interface TimelineSearchParams {
  time_range: string;
  event_type?: string;
  k?: number;
  agent_name?: string;
}

export async function searchMemoriesSemantic(
  params: SemanticSearchParams
): Promise<MemoryListResponse> {
  const response = await apiClient.post<MemoryListResponse>(
    '/memory/search-semantic',
    {
      query: params.query,
      k: params.k ?? 20,
      agent_name: params.agent_name ?? 'global',
    }
  );
  return response;
}

export async function searchMemoriesKeyword(
  params: KeywordSearchParams
): Promise<MemoryListResponse> {
  const response = await apiClient.post<MemoryListResponse>(
    '/memory/search-keyword',
    {
      query: params.query,
      k: params.k ?? 20,
      agent_name: params.agent_name ?? 'global',
    }
  );
  return response;
}

export async function searchMemoriesTimeline(
  params: TimelineSearchParams
): Promise<MemoryListResponse> {
  const response = await apiClient.post<MemoryListResponse>(
    '/memory/search-timeline',
    {
      time_range: params.time_range,
      event_type: params.event_type,
      k: params.k ?? 20,
      agent_name: params.agent_name ?? 'global',
    }
  );
  return response;
}
