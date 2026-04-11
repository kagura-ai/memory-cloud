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
 * If `allowedValues` is provided and non-empty, any URL value not in the
 * allowed set falls back to `defaultValue`. This prevents Radix Tabs from
 * rendering an empty panel when a user navigates to an unknown `?tab=` value.
 * An empty array is treated as if `allowedValues` were omitted.
 *
 * Note: when a page declares multiple `useTabParam` calls, each one MUST use
 * a distinct `paramName` — otherwise the calls share state via the same URL key.
 *
 * @param defaultValue - Fallback when the search param is absent or invalid
 * @param paramName - URL search param key (default `"tab"`)
 * @param allowedValues - Optional set of values the URL is allowed to carry
 *
 * @example
 * ```tsx
 * const TABS = ["overview", "connections", "settings"] as const;
 * const [tab, setTab] = useTabParam("overview", "tab", TABS);
 *
 * <Tabs value={tab} onValueChange={setTab}>
 *   <TabsTrigger value={TABS[0]}>Overview</TabsTrigger>
 *   ...
 * </Tabs>
 * ```
 */
export function useTabParam(
  defaultValue: string,
  paramName: string = "tab",
  allowedValues?: readonly string[],
): [string, (value: string) => void] {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();

  if (
    process.env.NODE_ENV === "development" &&
    allowedValues &&
    allowedValues.length > 0 &&
    !allowedValues.includes(defaultValue)
  ) {
    // eslint-disable-next-line no-console
    console.warn(
      `[useTabParam] defaultValue "${defaultValue}" is not in allowedValues ` +
        `[${allowedValues.join(", ")}]. The fallback will produce an empty Tabs panel.`,
    );
  }

  const raw = searchParams.get(paramName);
  const value = resolveTabValue(raw, defaultValue, allowedValues, paramName);

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

/**
 * Resolve the effective tab value from a raw URL param, applying validation
 * against `allowedValues` when provided. Exported for unit testing only.
 *
 * - `raw === null` (param absent) → `defaultValue`
 * - `allowedValues` undefined or empty → return `raw` as-is (backward compat)
 * - `raw` not in `allowedValues` → fall back to `defaultValue`, dev `console.warn`
 * - otherwise → return `raw`
 */
export function resolveTabValue(
  raw: string | null,
  defaultValue: string,
  allowedValues: readonly string[] | undefined,
  paramName: string,
): string {
  if (raw === null) return defaultValue;
  if (!allowedValues || allowedValues.length === 0) return raw;
  if (allowedValues.includes(raw)) return raw;

  if (process.env.NODE_ENV === "development") {
    // eslint-disable-next-line no-console
    console.warn(
      `[useTabParam] Unknown value "${raw}" for ?${paramName}=; ` +
        `falling back to "${defaultValue}". Allowed: [${allowedValues.join(", ")}]`,
    );
  }
  return defaultValue;
}
