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

import { buildOAuthRedirect } from "@/lib/auth/buildOAuthRedirect";
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
    // Build through the shared helper, not by concatenation. It guards three
    // traps this flow has no reason to re-learn: NEXT_PUBLIC_API_URL may
    // already carry an `/api/v1` suffix (yielding `/api/v1/api/v1/...`), the
    // backend redirects to `return_to` verbatim so it must be absolute and
    // same-origin (CWE-601), and the env var may end in trailing slashes.
    //
    // `add_account=1` is appended after: it makes the OAuth callback APPEND to
    // this session instead of replacing it. Without it there is never a second
    // account to switch to.
    const url = new URL(buildOAuthRedirect("google", "/"));
    url.searchParams.set("add_account", "1");
    window.location.assign(url.toString());
  }, []);

  return { accounts, switchingTo, refresh, switchTo, addAccount };
}
