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
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  status: string;
  created_at: string;
  uploaded_at: string | null;
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
): Promise<FileObject[]> {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    limit: limit.toString(),
  });
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
