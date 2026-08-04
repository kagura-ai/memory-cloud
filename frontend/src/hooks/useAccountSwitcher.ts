/**
 * Accounts signed in on this browser session, and how to move between them
 * (#1488 Phase 3).
 *
 * Extracted from the sidebar rather than inlined: every decision worth getting
 * right lives here (when to re-read, what to do on a rejected switch, how to
 * leave the page), while the menu itself is declarative JSX. That also makes
 * the behaviour testable without driving a Radix dropdown through a DOM
 * emulator, which this repo has no working pattern for.
 */
"use client";

import { useCallback, useState } from "react";

import {
  listAccounts,
  switchAccount,
  type SignedInAccount,
} from "@/lib/api/accounts";

export interface UseAccountSwitcher {
  accounts: SignedInAccount[];
  /** Non-null while a switch is in flight — disables the rows. */
  switchingTo: string | null;
  /** Re-read the list. Call on every menu open. */
  refresh: () => Promise<void>;
  switchTo: (userId: string) => Promise<void>;
  addAccount: () => void;
}

export function useAccountSwitcher(): UseAccountSwitcher {
  const [accounts, setAccounts] = useState<SignedInAccount[]>([]);
  const [switchingTo, setSwitchingTo] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setAccounts(await listAccounts());
    } catch {
      // An older backend has no /auth/accounts. Empty means the menu renders
      // exactly as it did before this feature — degraded, never broken.
      setAccounts([]);
    }
  }, []);

  const switchTo = useCallback(
    async (userId: string) => {
      setSwitchingTo(userId);
      try {
        await switchAccount(userId);
        // Hard navigation, deliberately. The active workspace is a per-user
        // column and several providers cache per-user data, so a client-side
        // refresh would leave the previous account's workspace and contexts on
        // screen under the new identity. A reload is the only honest reset.
        window.location.assign("/");
      } catch {
        // 404 means the account is no longer on this session (signed out in
        // another tab). Re-read instead of navigating, or we would land on a
        // page rendered for an identity this session no longer holds.
        setSwitchingTo(null);
        await refresh();
      }
    },
    [refresh],
  );

  const addAccount = useCallback(() => {
    // `add_account=1` makes the OAuth callback APPEND to this session instead
    // of replacing it. Without it there is never a second account to switch to.
    const returnTo = `${window.location.origin}/`;
    const base = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
    window.location.assign(
      `${base}/api/v1/auth/google/login?add_account=1&return_to=${encodeURIComponent(returnTo)}`,
    );
  }, []);

  return { accounts, switchingTo, refresh, switchTo, addAccount };
}
