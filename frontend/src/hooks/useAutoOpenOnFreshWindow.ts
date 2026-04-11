"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Auto-open a UI surface (e.g. a Collapsible) the first time a fresh
 * "visibility window" begins, but respect manual user closes thereafter.
 *
 * The hook tracks `currentExpiresAt` (an ISO timestamp string identifying
 * the visibility window). When the value transitions to a NEW non-null
 * timestamp (different from the previously seen value), the hook flips the
 * open state to `true`. The user can manually close it via the returned
 * setter; subsequent renders with the SAME timestamp will not re-open it.
 * Only a NEW timestamp (= the backend issued a fresh visibility window,
 * e.g. after key creation or regenerate) re-triggers the auto-open.
 *
 * Returns a `[isOpen, setIsOpen]` tuple compatible with shadcn/Radix
 * Collapsible's `open` / `onOpenChange` controlled-mode props.
 *
 * @param currentExpiresAt - The current visibility window's expiration ISO
 *   timestamp, or `null` when no window is active. Pass the value from your
 *   data source (e.g. `credentials?.api_keys?.[0]?.visibility_expires_at`).
 *
 * @example
 * ```tsx
 * const [setupOpen, setSetupOpen] = useAutoOpenOnFreshWindow(
 *   credentials?.api_keys?.[0]?.visibility_expires_at ?? null,
 * );
 *
 * <Collapsible open={setupOpen} onOpenChange={setSetupOpen}>
 *   ...
 * </Collapsible>
 * ```
 *
 * Semantics:
 * - Initial render with `null` → closed (no auto-open).
 * - Initial render with a non-null timestamp → opens (treated as a fresh
 *   window because the previously-seen value was `null` from the ref's
 *   initial state).
 * - Same timestamp on subsequent renders → no change (auto-refresh polling
 *   does not re-open after a deliberate close).
 * - Transition to `null` → no change (closing happens via user, not auto).
 * - Transition to a different non-null timestamp → opens (fresh window).
 *
 * The user-controlled close is preserved by the `useState` setter being
 * exposed: when the user calls `setIsOpen(false)`, the hook does NOT
 * override that on the next render unless a NEW timestamp arrives.
 */
export function useAutoOpenOnFreshWindow(
  currentExpiresAt: string | null,
): readonly [boolean, (open: boolean) => void] {
  // Initialize state from the FIRST prop value so the very first paint
  // already matches the documented semantics ("Initial render with a
  // non-null timestamp → opens"). The previous implementation used
  // useState(false) + useEffect(setOpen), which caused a one-paint
  // "closed → open" flash whenever the hook was mounted with a
  // non-null timestamp.
  //
  // The ref is initialized to the same first prop value so the effect's
  // "only auto-open on a NEW timestamp" comparison stays correct on the
  // first run (T !== T is false → no redundant setOpen). All other
  // semantics (manual close respected, fresh-window reopen, null→string
  // transition) are unaffected.
  const [isOpen, setIsOpen] = useState(currentExpiresAt !== null);
  const prevExpiresAtRef = useRef<string | null>(currentExpiresAt);

  useEffect(() => {
    if (currentExpiresAt && currentExpiresAt !== prevExpiresAtRef.current) {
      setIsOpen(true);
    }
    prevExpiresAtRef.current = currentExpiresAt;
  }, [currentExpiresAt]);

  return [isOpen, setIsOpen] as const;
}
