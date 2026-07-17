/** System-admin API for worker app identities (#1315). */

import { apiClient } from "./base";

export type WorkerAppPlatform = "slack" | "discord" | "teams";
export type WorkerAppStatus = "unconfigured" | "active" | "disabled";

export interface WorkerAppIdentity {
  platform: WorkerAppPlatform;
  app_key: string;
  display_name: string;
  status: WorkerAppStatus;
  revision: string;
  has_active_secret: boolean;
  active_secret_revision: number | null;
  retiring_secret_revision: number | null;
  retiring_valid_until: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateWorkerAppRequest {
  platform: WorkerAppPlatform;
  app_key: string;
  display_name: string;
  signing_secret: string;
}

export async function listWorkerApps(): Promise<WorkerAppIdentity[]> {
  return apiClient.get<WorkerAppIdentity[]>("/api/v1/admin/worker-apps");
}

export async function createWorkerApp(
  request: CreateWorkerAppRequest,
): Promise<WorkerAppIdentity> {
  return apiClient.post<WorkerAppIdentity>(
    "/api/v1/admin/worker-apps",
    request,
  );
}

export async function updateWorkerApp(
  app: Pick<WorkerAppIdentity, "platform" | "app_key">,
  request: { display_name?: string; status?: "active" | "disabled" },
): Promise<WorkerAppIdentity> {
  return apiClient.patch<WorkerAppIdentity>(
    `/api/v1/admin/worker-apps/${encodeURIComponent(app.platform)}/${encodeURIComponent(app.app_key)}`,
    request,
  );
}

export async function rotateWorkerAppSecret(
  app: Pick<WorkerAppIdentity, "platform" | "app_key">,
  signingSecret: string,
  retiringForSeconds = 3600,
): Promise<WorkerAppIdentity> {
  return apiClient.post<WorkerAppIdentity>(
    `/api/v1/admin/worker-apps/${encodeURIComponent(app.platform)}/${encodeURIComponent(app.app_key)}/rotate-secret`,
    {
      signing_secret: signingSecret,
      retiring_for_seconds: retiringForSeconds,
    },
  );
}
