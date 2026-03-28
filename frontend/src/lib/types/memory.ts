/**
 * Memory API Type Definitions
 *
 * TypeScript interfaces for Kagura Memory Cloud API
 */

export type MemoryScope = 'working' | 'persistent';
export type MemoryType = 'normal' | 'coding';

export interface Memory {
  key: string;
  value: string;
  scope: MemoryScope;
  type?: MemoryType;
  agent_name: string;
  user_id: string;
  importance: number;
  metadata?: Record<string, unknown>;
  tags?: string[];
  created_at: string;
  updated_at: string;
  access_count?: number;
  last_accessed?: string;
}

export interface CreateMemoryRequest {
  key: string;
  value: string;
  scope?: MemoryScope;
  type?: MemoryType;
  agent_name?: string;
  importance?: number;
  metadata?: Record<string, unknown>;
  tags?: string[];
}

export interface UpdateMemoryRequest {
  value?: string;
  type?: MemoryType;
  importance?: number;
  metadata?: Record<string, unknown>;
  tags?: string[];
}

export interface MemorySearchParams {
  query?: string;
  scope?: MemoryScope;
  agent_name?: string;
  tags?: string[];
  min_importance?: number;
  max_importance?: number;
  limit?: number;
  offset?: number;
}

export interface MemoryListResponse {
  memories: Memory[];
  total: number;
  limit: number;
  offset: number;
}

export interface MemoryStatsResponse {
  total_memories: number;
  working_memories: number;
  persistent_memories: number;
  total_size_bytes: number;
  avg_importance: number;
  agents: string[];
  tags: string[];
}


// ============================================================================
// Issue #720: Search result types
// ============================================================================

export interface SearchResultMemory extends Memory {
  score: number;  // Relevance score (RAG similarity or BM25 score)
  search_mode?: 'semantic' | 'keyword' | 'timeline';
}

export type SearchMode = 'simple' | 'semantic' | 'keyword' | 'timeline';
