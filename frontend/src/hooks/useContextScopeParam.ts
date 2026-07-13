"use client";

/**
 * useContextScopeParam — read/write the `?context_id=` deep-link parameter
 * for the admin memory-health drill-down (#1227).
 *
 * Returns `[scope, setScope]`. Unlike `useMemoryIdParam` (dialog state) and
 * `useTabParam` (tab facets), which use `router.replace`, scope changes here
 * are NAVIGATION between two distinct views: `setScope` uses `router.push`
 * so the browser back button returns from a context's detail to the
 * breakdown list and forward returns to the detail.
 *
 * The value is either a context UUID or the `"unattributed"` sentinel;
 * validation is the API's job — an unknown value surfaces as the detail
 * fetch's error banner, exactly like a stale bookmark.
 *
 * `setScope`'s identity changes whenever `searchParams` does (it closes over
 * the latest query string to preserve unrelated params) — same contract as
 * `useMemoryIdParam`; do not cache it expecting referential equality.
 */

import { useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

export const CONTEXT_SCOPE_PARAM = "context_id";

export function useContextScopeParam(): readonly [string | null, (scope: string | null) => void] {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const scope = searchParams.get(CONTEXT_SCOPE_PARAM);

  const setScope = useCallback(
    (next: string | null) => {
      // Same-value no-op: router.push commits in a transition, so a fast
      // double-click can land before the first navigation renders — without
      // this guard it pushes a duplicate identical history entry and the
      // first browser Back press appears dead.
      if (next === searchParams.get(CONTEXT_SCOPE_PARAM)) {
        return;
      }
      const params = new URLSearchParams(searchParams.toString());
      if (next) {
        params.set(CONTEXT_SCOPE_PARAM, next);
      } else {
        params.delete(CONTEXT_SCOPE_PARAM);
      }
      const qs = params.toString();
      router.push(`${pathname}${qs ? `?${qs}` : ""}`);
    },
    [pathname, router, searchParams],
  );

  return [scope, setScope] as const;
}
