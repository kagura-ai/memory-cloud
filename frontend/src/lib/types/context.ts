/**
 * Context types for multi-collection support.
 *
 * Issue #82: Context-based Multi-Collection Support
 */

export type SleepMode = "full" | "edges_only" | "skip";

export interface Context {
  id: string;
  name: string;
  display_name: string | null;
  description: string | null;
  summary: string | null;
  usage_guide: string | null;
  collection_name: string;
  is_default: boolean;
  // Issue #246: is_current removed (context always explicit from URL)
  is_private: boolean; // Issue #165: Privacy control (shared/private)
  is_public: boolean; // Issue #238: Public context flag (external access)
  is_locked: boolean; // Issue #85: Lock flag (prevent deletion)
  sleep_mode: SleepMode;
  resource_id: string | null; // Issue #238: Resource-backed context
  created_by: string | null; // Issue #165: Creator user_id
  created_by_name: string | null; // Creator name
  created_at: string;
  updated_at: string | null;
  // Issue #217: Search config summary
  use_rerank: boolean | null;
  reranker_provider: string | null; // 'voyage' | 'cohere'
  // Embedding model info (Issue #49)
  embedding_model: string | null;
  embedding_dimensions: number | null;
  // Context members count
  member_count: number | null;
  // Issue #187: Memory stats for contexts table
  memory_count: number;
  last_activity_at: string | null;
}

export interface ContextListResponse {
  contexts: Context[];
  // Issue #246: current_context_id removed
  total: number;
}

export interface ContextStats {
  context_id: string;
  context_name: string;
  collection_name: string;
  points_count: number;
  vectors_count: number;
  status: string;
}

export interface CreateContextRequest {
  name: string;
  display_name?: string;
  description?: string;
  summary?: string;
  usage_guide?: string;
  embedding_model?: string; // Issue #49: Embedding model selection
  is_private?: boolean; // Issue #165: Privacy control (default: true)
}

export interface UpdateContextRequest {
  display_name?: string;
  description?: string;
  summary?: string;
  usage_guide?: string;
  is_private?: boolean; // Issue #165: Privacy control (shared/private)
  is_public?: boolean; // Issue #238: Public context flag (external access)
  resource_id?: string; // Issue #238: Resource ID (auto-generated: prefix_contextId)
  is_locked?: boolean; // Issue #85: Lock flag (prevent deletion)
  sleep_mode?: SleepMode;
}
