"use client";

/**
 * useMemoryIdParam — read/write the `?memoryId=` deep-link parameter.
 *
 * Returns `[memoryIdParam, setMemoryIdParam]`. `setMemoryIdParam`'s identity
 * changes whenever `searchParams` does (i.e. on every URL query change),
 * because the callback closes over the latest query string to preserve other
 * params. Treat it the same as React's own `useSearchParams` hook for
 * dependency arrays — pass it explicitly when consumers need to react to
 * URL-driven changes; do NOT cache it across renders expecting referential
 * equality. Uses `router.replace` (not push) so dialog open/close cycles
 * don't pollute browser history.
 */

import { useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

export const MEMORY_ID_PARAM = "memoryId";

export function useMemoryIdParam(): readonly [
  string | null,
  (id: string | null) => void,
] {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const memoryIdParam = searchParams.get(MEMORY_ID_PARAM);

  const setMemoryIdParam = useCallback(
    (id: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (id) {
        params.set(MEMORY_ID_PARAM, id);
      } else {
        params.delete(MEMORY_ID_PARAM);
      }
      const qs = params.toString();
      router.replace(`${pathname}${qs ? `?${qs}` : ""}`);
    },
    [pathname, router, searchParams],
  );

  return [memoryIdParam, setMemoryIdParam] as const;
}
