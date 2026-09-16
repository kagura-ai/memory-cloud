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
 * - Remount-safe (#1532): the consumed params are also remembered per
 *   `cleanUrl` outside the component, so a parent that unmounts and remounts
 *   the page while the `router.replace` is still in flight does not
 *   re-consume the same params. The memory is dropped as soon as the observed
 *   params differ (the strip landed), so the *next* arrival of identical
 *   params — a genuinely new event — is consumed again.
 * - `consume` returning `false` leaves the params untouched and retries on
 *   the next params change.
 * - The latest `consume` closure is always used (ref-forwarded), so inline
 *   callbacks capturing fresh state are safe without effect-dep churn.
 *
 * Deliberately NOT used by the login page: its `?error=` banner is
 * URL-state-driven (never stripped, re-shows on refresh) — state, not a
 * one-shot event.
 */
/**
 * Last consumed params per `cleanUrl`, kept outside React so it survives a
 * remount of the consuming page (#1532). Entries are short-lived: cleared the
 * moment a hook instance observes different params for that `cleanUrl`, and
 * ignored once older than the remount race they exist to cover — so a stale
 * entry (the page was left before the strip landed) can never swallow a later,
 * genuine arrival of the same params.
 */
const lastConsumed = new Map<string, { key: string; at: number }>();
const REMEMBER_MS = 10_000;

/** Test hook: forget every remembered consume. */
export function resetConsumedSearchParams(): void {
  lastConsumed.clear();
}

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
    const key = searchParams.toString();
    const remembered = lastConsumed.get(cleanUrl);
    const previous =
      remembered && Date.now() - remembered.at < REMEMBER_MS ? remembered.key : undefined;
    if (remembered !== undefined && previous !== key) {
      // The URL moved on (the strip landed, or new params arrived) or the
      // memory expired: it no longer describes the current URL.
      lastConsumed.delete(cleanUrl);
    }
    if (handled.current) return;
    if (previous === key) {
      // Remounted while the strip is still in flight — already handled.
      handled.current = true;
      return;
    }
    if (!enabled) return;
    if (!consumeRef.current(searchParams)) return;
    handled.current = true;
    lastConsumed.set(cleanUrl, { key, at: Date.now() });
    router.replace(cleanUrl);
  }, [searchParams, enabled, cleanUrl, router]);
}
