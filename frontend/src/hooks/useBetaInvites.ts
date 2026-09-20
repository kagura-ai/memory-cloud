"use client";

/**
 * useBetaInvites (#1582, #1595)
 *
 * The caller's beta-invite quota and invites — ONE instance (owned by the
 * sidebar) feeds the "Invite a friend" card, the account-menu entry and the
 * dialog, so creating, revoking or reissuing an invite updates all three at
 * once. The one-time URL a mutation returns is handed to the caller and never
 * kept here.
 *
 * Makes no request unless `features.beta_invites === true`: every
 * `/beta-invites` route 404s while the feature is off, and an older backend
 * sends no flag at all. `summary` stays `null` until the first load resolves;
 * callers render nothing invite-related from a `null` summary (no
 * flash-then-hide, #1571).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api/base";
import {
  createBetaInvite,
  getMyBetaInvites,
  reissueBetaInvite,
  revokeBetaInvite,
  type BetaInviteCreated,
  type BetaInviteSummary,
} from "@/lib/api/beta-invites";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";

export interface UseBetaInvites {
  /** `null` until loaded — and always while the feature is off. */
  summary: BetaInviteSummary | null;
  isLoading: boolean;
  /** The last failed load; cleared by the next successful one. */
  error: unknown;
  refresh: () => Promise<void>;
  /** Mints an invite. The returned URL is a credential, shown once. */
  create: (label?: string) => Promise<BetaInviteCreated>;
  revoke: (id: string) => Promise<void>;
  /** Revokes `id` and mints its replacement. Same one-time URL rules. */
  reissue: (id: string) => Promise<BetaInviteCreated>;
}

export function useBetaInvites(): UseBetaInvites {
  const enabled = useSystemFeatures()?.beta_invites === true;
  const [summary, setSummary] = useState<BetaInviteSummary | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const aliveRef = useRef(true);
  // Only the latest load may write state — a slow earlier response must not
  // overwrite the numbers a later mutation already refreshed.
  const latestLoadRef = useRef(0);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!enabled) return;
    const load = ++latestLoadRef.current;
    const isCurrent = () => aliveRef.current && load === latestLoadRef.current;
    setIsLoading(true);
    try {
      const next = await getMyBetaInvites();
      if (!isCurrent()) return;
      setSummary(next);
      setError(null);
    } catch (e) {
      // Keep the last known summary: stale numbers beat a vanishing dialog.
      if (isCurrent()) setError(e);
    } finally {
      if (isCurrent()) setIsLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A 409 means the numbers on screen are stale (the cap was reached from
  // another tab, or the invite was redeemed meanwhile) — re-read before the
  // caller shows its message.
  const create = useCallback(
    async (label?: string) => {
      try {
        const created = await createBetaInvite(label);
        await refresh();
        return created;
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) await refresh();
        throw e;
      }
    },
    [refresh],
  );

  const revoke = useCallback(
    async (id: string) => {
      try {
        await revokeBetaInvite(id);
        await refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) await refresh();
        throw e;
      }
    },
    [refresh],
  );

  // Same 409 rule: already used, already revoked (a double-click — the first
  // request won) or at the cap all mean the list on screen is out of date.
  const reissue = useCallback(
    async (id: string) => {
      try {
        const created = await reissueBetaInvite(id);
        await refresh();
        return created;
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) await refresh();
        throw e;
      }
    },
    [refresh],
  );

  return { summary, isLoading, error, refresh, create, revoke, reissue };
}
