/**
 * Coding Sessions API Client
 *
 * Provides methods for fetching coding session information.
 * Issue #666: Phase 2 - Memory List Enhancement
 */

import { apiClient } from './base';

// Type definitions matching Backend Pydantic models

export interface FileChange {
  file_path: string;
  action: string;
  diff?: string;
  reason?: string;
  line_range?: string;
}

export interface Decision {
  decision: string;
  rationale: string;
  alternatives: string[];
  impact?: string;
  tags: string[];
}

export interface ErrorRecord {
  error_type: string;
  message: string;
  file_path?: string;
  line_number?: number;
  solution?: string;
}

export interface SessionSummary {
  id: string;
  context_id: string;
  description: string;
  start_time: string;
  end_time?: string;
  duration_seconds?: number;
  file_changes_count: number;
  decisions_count: number;
  errors_count: number;
  github_issue?: number;
  success?: boolean;
}

export interface SessionDetailResponse {
  session: SessionSummary;
  file_changes: FileChange[];
  decisions: Decision[];
  errors: ErrorRecord[];
}

export interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
  page: number;
  page_size: number;
}

/**
 * List coding sessions
 */
export async function listCodingSessions(params: {
  context_id?: string;
  limit?: number;
  offset?: number;
}): Promise<SessionListResponse> {
  const searchParams = new URLSearchParams();
  if (params.context_id) searchParams.set('context_id', params.context_id);
  if (params.limit) searchParams.set('limit', params.limit.toString());
  if (params.offset) searchParams.set('offset', params.offset.toString());

  const query = searchParams.toString() ? `?${searchParams.toString()}` : '';
  return apiClient.get<SessionListResponse>(`/coding/sessions${query}`);
}

/**
 * Get coding session detail
 */
export async function getCodingSessionDetail(
  sessionId: string
): Promise<SessionDetailResponse> {
  return apiClient.get<SessionDetailResponse>(`/coding/sessions/${sessionId}`);
}

/**
 * Format duration in human-readable format
 */
export function formatDuration(seconds: number | undefined): string {
  if (!seconds) return 'N/A';

  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}

/**
 * Format date for display
 */
export function formatSessionDate(dateString: string): string {
  const date = new Date(dateString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return `${diffDays} days ago`;

  return date.toLocaleDateString();
}
