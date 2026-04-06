/**
 * Shared types and helpers for Sleep Report admin pages.
 * Used by /admin/sleep-reports list and detail pages.
 */

export type SleepStatus =
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "rolled_back";

export const SLEEP_STATUS_OPTIONS: SleepStatus[] = [
  "completed",
  "running",
  "failed",
  "cancelled",
  "rolled_back",
];

export function getSleepStatusColor(status: SleepStatus): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300";
    case "running":
      return "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300";
    case "failed":
      return "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300";
    case "rolled_back":
      return "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300";
    case "cancelled":
    default:
      return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300";
  }
}
