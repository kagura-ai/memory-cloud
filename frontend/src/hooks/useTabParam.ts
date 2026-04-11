"use client";

import { useCallback, useEffect } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

/**
 * Sync the active tab with a URL search parameter.
 *
 * Returns `[value, setValue]` compatible with
 * `<Tabs value={value} onValueChange={setValue}>`.
 *
 * Uses `router.replace` so tab switches don't pollute browser history.
 *
 * The URL always carries an explicit `?<paramName>=<value>` once mounted —
 * if the URL is loaded without the param (e.g. `/credentials`), the hook
 * promotes it to `/credentials?tab=<defaultValue>` on first render. This
 * keeps the URL canonical so the sidebar (and any other URL-driven UI) can
 * detect the active tab via simple string comparison without having to know
 * each page's default value.
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
      params.set(paramName, newValue);
      router.replace(`${pathname}?${params.toString()}`);
    },
    [searchParams, pathname, router, paramName],
  );

  // Promote the canonical URL form on first mount when the param is missing.
  // This keeps the URL in sync with the rendered tab so external selectors
  // (sidebar isActive, deep links, browser bookmarks) work without having to
  // know the page's default tab. Invalid values (raw not in allowedValues)
  // are intentionally NOT auto-promoted — the URL is corrected on the next
  // user interaction, preserving forensic info. The dev warn in resolveTabValue
  // will re-fire on each render until the URL is corrected, by design: a
  // persistent warning is more visible to developers than a one-shot.
  useEffect(() => {
    if (raw === null) {
      setValue(defaultValue);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
      `[useTabParam] Unknown ${paramName} value "${raw}"; ` +
        `falling back to "${defaultValue}". Allowed: [${allowedValues.join(", ")}]`,
    );
  }
  return defaultValue;
}
