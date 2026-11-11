/**
 * Memory health report API client (#1211, #1225, #2764).
 *
 * `GET /api/v1/memory-health` grades the contexts the caller created
 * (session only). Without a scope it returns the per-context breakdown; with a
 * context id, or the `unattributed` sentinel, the three-section detail.
 */

import { apiClient } from "./base";

export type MemoryHealthStatus = "ok" | "warn" | "fail";

export interface MemoryHealthNote {
  code: string;
  params: Record<string, unknown>;
}

export interface MemoryHealthSection {
  status: MemoryHealthStatus;
  metrics: Record<string, unknown>;
  notes: MemoryHealthNote[];
}

export interface MemoryHealthContextEntry {
  /** The context id, or null for the unattributed bucket. */
  context_id: string | null;
  name: string | null;
  overall_status: MemoryHealthStatus;
  sections: Record<string, MemoryHealthStatus>;
}

export interface MemoryHealthBreakdown {
  generated_at: string;
  overall_status: MemoryHealthStatus;
  contexts: MemoryHealthContextEntry[];
}

export interface MemoryHealthDetail {
  generated_at: string;
  context_id: string | null;
  context_name: string | null;
  overall_status: MemoryHealthStatus;
  sections: Record<string, MemoryHealthSection>;
}

/** Scope value selecting the signals recorded outside the caller's contexts. */
export const UNATTRIBUTED_SCOPE = "unattributed";

const BASE = "/api/v1/memory-health";

export async function getMemoryHealthBreakdown(): Promise<MemoryHealthBreakdown> {
  return apiClient.get<MemoryHealthBreakdown>(BASE);
}

/** `scope` is a context id or {@link UNATTRIBUTED_SCOPE}. */
export async function getMemoryHealthDetail(scope: string): Promise<MemoryHealthDetail> {
  return apiClient.get<MemoryHealthDetail>(
    `${BASE}?context_id=${encodeURIComponent(scope)}`,
  );
}
