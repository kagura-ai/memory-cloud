/**
 * One-shot stash for the auto-switch toast (Issue #660).
 *
 * Written by `/workspace/settings/general` right before `deleteWorkspace()`,
 * read once by `/workspace/dashboard` on mount, then cleared. localStorage is
 * wrapped in try/catch because private mode and quota errors are non-blocking
 * for this UX-only feature.
 */

import { RECENTLY_DELETED_WORKSPACE_KEY } from "@/lib/constants/storage-keys";

export interface RecentlyDeletedWorkspace {
  id: string;
  name: string;
  ts: number;
}

export function writeRecentlyDeletedWorkspace(
  value: RecentlyDeletedWorkspace,
): void {
  try {
    window.localStorage.setItem(
      RECENTLY_DELETED_WORKSPACE_KEY,
      JSON.stringify(value),
    );
  } catch {
    // localStorage may be unavailable (private mode, quota). Non-blocking.
  }
}

export function readRecentlyDeletedWorkspace(): RecentlyDeletedWorkspace | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(RECENTLY_DELETED_WORKSPACE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;

  try {
    const parsed = JSON.parse(raw) as {
      id?: unknown;
      name?: unknown;
      ts?: unknown;
    };
    if (
      typeof parsed?.id !== "string" ||
      typeof parsed?.name !== "string" ||
      typeof parsed?.ts !== "number"
    ) {
      return null;
    }
    return { id: parsed.id, name: parsed.name, ts: parsed.ts };
  } catch {
    return null;
  }
}

export function clearRecentlyDeletedWorkspace(): void {
  try {
    window.localStorage.removeItem(RECENTLY_DELETED_WORKSPACE_KEY);
  } catch {
    // ignore
  }
}
