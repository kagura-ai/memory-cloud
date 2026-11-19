/**
 * Error codes of the workspace allowance API (#2918) the page acts on.
 */

import { ApiError } from "@/lib/api/base";
import type { WorkspaceSlotsFullDetails } from "@/lib/api/workspaceSlots";

/** The selection was saved against an older `version`. */
export const SLOT_STALE = "SLOT-003";
/** No active slot can be freed for a suspended workspace. */
const SLOT_FULL = "SLOT-002";
/** A running paid subscription blocks deleting the workspace (#1940). */
export const BILLING_CONTRACT_ACTIVE = "BILLING-005";

export function errorCode(error: unknown): string | undefined {
  return error instanceof ApiError ? error.error : undefined;
}

/** The remedies of a `SLOT-002`, or null for any other error. */
export function slotsFullDetails(
  error: unknown,
): WorkspaceSlotsFullDetails | null {
  if (errorCode(error) !== SLOT_FULL) return null;
  return ((error as ApiError).details ?? {}) as WorkspaceSlotsFullDetails;
}

/**
 * Another change for the same owner is being applied (409 RES-002 with
 * `details.reason: "workspace_slots_busy"`): a retry in a moment works.
 */
export function isSlotsBusy(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.error === "RES-002" &&
    error.details?.reason === "workspace_slots_busy"
  );
}

/** A 404 from the account API: the deployment has the feature off. */
export function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}
