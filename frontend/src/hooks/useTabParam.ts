"use client";

import { useCallback } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

/**
 * Sync the active tab with a URL search parameter.
 *
 * Returns `[value, setValue]` compatible with
 * `<Tabs value={value} onValueChange={setValue}>`.
 *
 * Uses `router.replace` so tab switches don't pollute browser history.
 *
 * @param defaultValue - Fallback when the search param is absent
 * @param paramName - URL search param key (default `"tab"`)
 *
 * @example
 * ```tsx
 * const [tab, setTab] = useTabParam("overview");
 * // URL: /page          → tab = "overview"
 * // URL: /page?tab=settings → tab = "settings"
 *
 * <Tabs value={tab} onValueChange={setTab}>
 *   ...
 * </Tabs>
 * ```
 */
export function useTabParam(
  defaultValue: string,
  paramName: string = "tab",
): [string, (value: string) => void] {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();

  const value = searchParams.get(paramName) ?? defaultValue;

  const setValue = useCallback(
    (newValue: string) => {
      const params = new URLSearchParams(searchParams.toString());
      if (newValue === defaultValue) {
        params.delete(paramName);
      } else {
        params.set(paramName, newValue);
      }
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname);
    },
    [searchParams, pathname, router, defaultValue, paramName],
  );

  return [value, setValue];
}
