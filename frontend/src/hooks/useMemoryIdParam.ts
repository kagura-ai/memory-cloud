"use client";

/**
 * useMemoryIdParam — read/write the `?memoryId=` deep-link parameter.
 *
 * Returns a stable `[memoryIdParam, setMemoryIdParam]` tuple that other tabs
 * (Memories, Graph, …) share so a memory opened via the URL stays open when
 * the user switches tabs. Uses `router.replace` (not push) so dialog
 * open/close cycles don't pollute browser history.
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
