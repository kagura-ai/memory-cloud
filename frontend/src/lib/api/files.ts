/**
 * File Objects Client (Issue #955)
 *
 * Thin wrappers over the platform file-storage REST API
 * (`backend/src/api/routes/files.py`). All endpoints are workspace-scoped:
 * `workspace_id` is passed as a query parameter and verified server-side
 * against the caller's membership.
 *
 * Roles (mirrors backend authz):
 *   - list / download-url → viewer+
 *   - delete              → member+
 */

import { apiClient } from "./base";

/**
 * Subset of the backend `FileObject` exposed to clients
 * (`FileObjectOut` in `files.py`). Note there is intentionally no
 * `created_by` field on the response, so the UI does not surface an
 * uploader column — adding one would require a backend change, which is
 * out of scope for this frontend-only wire-up.
 */
export interface FileObject {
  id: string;
  workspace_id: string;
  /**
   * Owning context (Issue #1136). `null` = workspace-scoped (any workspace
   * viewer can read it). When set, access is routed through the context's ACL,
   * and the listing only includes files in contexts the caller can access.
   */
  context_id: string | null;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  status: string;
  /**
   * Malware scan state (#565). `unscanned`: uploaded while scanning was off
   * (downloadable); `pending`: being scanned, the download URL is refused
   * (`SCAN-001`) until it finishes; `clean`; `infected` / `error`: blocked
   * (`SCAN-002`).
   */
  scan_status: ScanStatus;
  created_at: string;
  uploaded_at: string | null;
}

export type ScanStatus = "unscanned" | "pending" | "clean" | "infected" | "error";

/** Backend codes of the download-URL refusals while scanning holds a file (#565). */
export const SCAN_PENDING_ERROR = "SCAN-001";
export const SCAN_BLOCKED_ERROR = "SCAN-002";

/** Whether the malware scan blocked the file for good (no download). */
export function isScanBlocked(file: Pick<FileObject, "scan_status">): boolean {
  return file.scan_status === "infected" || file.scan_status === "error";
}

interface FileDownloadUrlOut {
  download_url: string;
}

/**
 * List uploaded, non-deleted files in the workspace, newest first.
 *
 * The backend caps results at `limit` (1–500, default 50) and returns no
 * total count, so callers detect "there may be more" by checking whether
 * the returned length equals the requested limit.
 */
export async function listFiles(
  workspaceId: string,
  limit: number = 50,
  filter?: { scope?: "all" | "workspace"; contextId?: string },
): Promise<FileObject[]> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    limit: limit.toString(),
  });
  if (filter?.scope) params.set("scope", filter.scope);
  if (filter?.contextId) params.set("context_id", filter.contextId);
  return apiClient.get<FileObject[]>(`/api/v1/files?${params.toString()}`);
}

/**
 * Resolve a short-lived presigned GET URL for a file (viewer+).
 */
export async function getDownloadUrl(
  workspaceId: string,
  fileId: string,
): Promise<string> {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  const res = await apiClient.get<FileDownloadUrlOut>(
    `/api/v1/files/${fileId}/download-url?${params.toString()}`,
  );
  return res.download_url;
}

export interface ContextFilesDeleteResult {
  deleted: number;
  released_bytes: number;
}

/**
 * Soft-delete every file of a context and release their quota (context owner).
 *
 * #1960: the way an owner clears a context's files in bulk — the default
 * context cannot be deleted, and the capacity lock counts file bytes.
 */
export async function deleteContextFiles(
  contextId: string,
): Promise<ContextFilesDeleteResult> {
  return apiClient.delete<ContextFilesDeleteResult>(
    `/api/v1/contexts/${contextId}/files`,
  );
}

/**
 * Soft-delete a file and release its quota (member+).
 */
export async function deleteFile(
  workspaceId: string,
  fileId: string,
): Promise<void> {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  await apiClient.delete<void>(`/api/v1/files/${fileId}?${params.toString()}`);
}
