"use client";

/**
 * useFocusedClusterId — URL-synced cluster focus state for #497.
 *
 * Owns the ``focusedClusterId: number | null`` state for the analyses
 * tab. Reads / writes the ``?cluster=N`` URL search param so:
 *
 *  - Browser back / forward restore the previous focus.
 *  - Deep-links like ``?tab=analyses&cluster=3`` open with cluster 3
 *    pre-focused, sharable across users.
 *  - The state is single source of truth — no duplicate React state
 *    in children that could drift out of sync.
 *
 * Validation: the ``allowedIndexes`` argument lets the caller specify
 * which cluster indices are valid for the active run. Invalid values
 * (cluster_index that no longer exists in the run, NaN, negative)
 * silently fall back to ``null`` and a single ``console.warn`` fires
 * in development. Production silently ignores — the UX guidance for
 * #497 is to render the "All clusters" view rather than a broken
 * focus state.
 */

import { useCallback, useEffect, useMemo } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

const CLUSTER_PARAM = "cluster";

/**
 * Parse a raw URL value to a non-negative integer. Returns null on
 * empty / NaN / negative — the caller decides what to do with null.
 */
export function parseClusterParam(raw: string | null): number | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (trimmed.length === 0) return null;
  const n = Number(trimmed);
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < 0) return null;
  return n;
}

interface UseFocusedClusterIdResult {
  /** Currently focused cluster index, or null when "All clusters" is active. */
  focusedClusterId: number | null;
  /** Set the focused cluster (or null to clear). Updates the URL. */
  setFocusedClusterId: (next: number | null) => void;
  /** Toggle: if the given index is already focused, clear; otherwise focus it. */
  toggleFocusedClusterId: (next: number) => void;
}

/**
 * Hook signature designed for AnalysesTabPanel:
 *
 *   const { focusedClusterId, setFocusedClusterId, toggleFocusedClusterId } =
 *     useFocusedClusterId(allowedIndexes);
 *
 * Pass an empty array on first paint (clusters not loaded yet) — the
 * hook then accepts any URL value as ``null`` (no validation possible
 * yet) and re-evaluates once the cluster list resolves and the
 * caller passes the populated array.
 */
export function useFocusedClusterId(
  allowedIndexes: readonly number[],
): UseFocusedClusterIdResult {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const raw = searchParams.get(CLUSTER_PARAM);

  // Memo allowedIndexes by content so callers passing a fresh array
  // each render don't trigger a useEffect storm. Cluster index lists
  // are tiny (typically ≤ 20), JSON.stringify is cheap.
  const allowedKey = useMemo(
    () => JSON.stringify(allowedIndexes),
    [allowedIndexes],
  );

  const focusedClusterId = useMemo<number | null>(() => {
    const parsed = parseClusterParam(raw);
    if (parsed === null) return null;
    if (allowedIndexes.length === 0) {
      // Allow-list not yet known — defer validation. Treat as null
      // so children don't render a focus mode for a cluster that may
      // not exist; the URL value is preserved (we don't strip it).
      return null;
    }
    if (!allowedIndexes.includes(parsed)) return null;
    return parsed;
    // allowedKey is the stable shape signal; using it instead of the
    // raw array reference avoids re-running on identical content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [raw, allowedKey]);

  const setFocusedClusterId = useCallback(
    (next: number | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (next === null) {
        params.delete(CLUSTER_PARAM);
      } else {
        params.set(CLUSTER_PARAM, String(next));
      }
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname);
    },
    [searchParams, pathname, router],
  );

  const toggleFocusedClusterId = useCallback(
    (next: number) => {
      setFocusedClusterId(focusedClusterId === next ? null : next);
    },
    [focusedClusterId, setFocusedClusterId],
  );

  // Strip an invalid ?cluster=N from the URL once the cluster list
  // has resolved — keeps the browser address bar honest after the
  // user navigated to a stale deep-link. Only strips after the list
  // is known (allowedIndexes.length > 0); during first paint we
  // preserve the URL so the focus can resolve once data arrives.
  useEffect(() => {
    if (raw === null) return;
    if (allowedIndexes.length === 0) return;
    const parsed = parseClusterParam(raw);
    if (parsed !== null && allowedIndexes.includes(parsed)) return;
    setFocusedClusterId(null);
    if (process.env.NODE_ENV === "development") {
      // eslint-disable-next-line no-console
      console.warn(
        `[useFocusedClusterId] dropping invalid ?cluster=${raw} (allowed: [${[...allowedIndexes].join(", ")}])`,
      );
    }
    // raw + allowedKey is the stable signal — a fresh allowedIndexes
    // array reference with identical content should not re-run.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [raw, allowedKey]);

  return { focusedClusterId, setFocusedClusterId, toggleFocusedClusterId };
}
