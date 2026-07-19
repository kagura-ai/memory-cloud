"use client";

import { useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { ReadonlyURLSearchParams } from "next/navigation";

export interface ConsumeSearchParamsOptions {
  /**
   * Gate: the consume attempt is deferred until this is true (default true).
   * Keeps page-specific readiness (RBAC flags, loaded user) at the call site.
   */
  enabled?: boolean;
  /** URL to `router.replace` to after a successful consume (strips the params). */
  cleanUrl: string;
}

/**
 * Consume one-shot search params: read → act → strip (#1382).
 *
 * The backend communicates OAuth/link outcomes by redirecting with query
 * params (`?slack_error=…`, `?refreshed=1`, …). Pages surface a toast and
 * must then strip the params so refresh/back doesn't re-trigger the notice.
 * This hook owns the shared mechanics; the page keeps only its `consume`
 * callback: inspect the params, act, and return `true` when handled.
 *
 * Semantics:
 * - Exactly-once: after a successful consume the group is marked handled via
 *   a ref, so React strict-mode double-invoked effects (and later param
 *   changes) cannot re-fire the notice.
 * - `consume` returning `false` leaves the params untouched and retries on
 *   the next params change.
 * - The latest `consume` closure is always used (ref-forwarded), so inline
 *   callbacks capturing fresh state are safe without effect-dep churn.
 *
 * Deliberately NOT used by the login page: its `?error=` banner is
 * URL-state-driven (never stripped, re-shows on refresh) — state, not a
 * one-shot event.
 */
export function useConsumeSearchParams(
  consume: (params: ReadonlyURLSearchParams) => boolean,
  { enabled = true, cleanUrl }: ConsumeSearchParamsOptions,
): void {
  const searchParams = useSearchParams();
  const router = useRouter();
  const handled = useRef(false);
  const consumeRef = useRef(consume);
  consumeRef.current = consume;

  useEffect(() => {
    if (handled.current) return;
    if (!enabled) return;
    if (!consumeRef.current(searchParams)) return;
    handled.current = true;
    router.replace(cleanUrl);
  }, [searchParams, enabled, cleanUrl, router]);
}
