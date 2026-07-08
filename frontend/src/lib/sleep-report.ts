/**
 * Shared types and helpers for Sleep Report admin pages.
 * Used by /admin/sleep-reports list and detail pages.
 */

export type SleepStatus =
  "running" | "completed" | "degraded" | "failed" | "cancelled" | "rolled_back";

export const SLEEP_STATUS_OPTIONS: SleepStatus[] = [
  "completed",
  "running",
  "degraded",
  "failed",
  "cancelled",
  "rolled_back",
];

/**
 * #1201: label for the user partition a Sleep run belongs to.
 *
 * Sleep runs per (user_id, workspace_id, context_id), so a workspace-scoped
 * list can show the same context on multiple rows. Prefer the resolved email;
 * fall back to a shortened user_id when the backend could not resolve it (a
 * connector/service identity absent from the users table) so the row is still
 * distinguishable without dumping a full opaque id.
 */
export function formatUserPartitionLabel(
  userEmail: string | null | undefined,
  userId: string,
): string {
  if (userEmail) return userEmail;
  return `uid:${userId.slice(0, 8)}`;
}

export function getSleepStatusColor(status: SleepStatus): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300";
    case "running":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300";
    case "degraded":
      // #1183: finished, but some judge-LLM calls failed — partial results.
      return "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300";
    case "failed":
      return "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300";
    case "rolled_back":
      return "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300";
    case "cancelled":
    default:
      return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300";
  }
}
