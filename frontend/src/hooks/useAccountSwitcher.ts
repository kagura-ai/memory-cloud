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
import { clearIdentityScopedClientState } from "@/lib/auth/clearClientState";
import { getAuthConfig } from "@/lib/auth/auth";
import {
  listAccounts,
  switchAccount,
  type SignedInAccount,
} from "@/lib/api/accounts";

/** OAuth providers a second account can be added with. */
export type AddableProvider = "google" | "github";

export interface UseAccountSwitcher {
  accounts: SignedInAccount[];
  /**
   * Which providers this deployment can actually add an account with.
   *
   * Empty until read, and empty on a deployment with no OAuth configured —
   * the menu must offer nothing rather than an item that navigates the whole
   * tab onto a raw 500 JSON page.
   */
  addableProviders: AddableProvider[];
  /** Non-null while a switch is in flight — disables the rows. */
  switchingTo: string | null;
  /** Re-read the list. Call on every menu open. */
  refresh: () => Promise<void>;
  switchTo: (userId: string) => Promise<void>;
  addAccount: (provider: AddableProvider) => void;
}

export function useAccountSwitcher(): UseAccountSwitcher {
  const [accounts, setAccounts] = useState<SignedInAccount[]>([]);
  const [addableProviders, setAddableProviders] = useState<AddableProvider[]>(
    [],
  );
  const [switchingTo, setSwitchingTo] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setAccounts(await listAccounts());
    } catch {
      // An older backend has no /auth/accounts. Empty means the menu renders
      // exactly as it did before this feature — degraded, never broken.
      setAccounts([]);
    }
    try {
      // Read alongside the account list rather than assuming Google. The OSS
      // default is password login with no OAuth client configured at all, and
      // the login endpoint answers 500 in that case — as a full-page
      // navigation, that throws the user out of the SPA onto raw JSON.
      const config = await getAuthConfig();
      const available: AddableProvider[] = [];
      if (config.google_oauth_enabled) available.push("google");
      if (config.github_oauth_enabled) available.push("github");
      setAddableProviders(available);
    } catch {
      // Offer nothing rather than something that might 500.
      setAddableProviders([]);
    }
  }, []);

  const switchTo = useCallback(
    async (userId: string) => {
      setSwitchingTo(userId);
      try {
        await switchAccount(userId);
        // The reload below resets React state, but not localStorage — keys
        // written without a user id (the workspace preselect, onboarding
        // progress) would otherwise follow the previous account into this one
        // (#1488 Phase 4).
        clearIdentityScopedClientState();
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

  const addAccount = useCallback((provider: AddableProvider) => {
    // The identity is about to change, so the same client state a switch or a
    // sign-out drops has to go here too — otherwise the workspace preselect and
    // onboarding progress of the account we are leaving greet a brand-new one.
    clearIdentityScopedClientState();

    // Build through the shared helper, not by concatenation. It guards three
    // traps this flow has no reason to re-learn: NEXT_PUBLIC_API_URL may
    // already carry an `/api/v1` suffix (yielding `/api/v1/api/v1/...`), the
    // backend redirects to `return_to` verbatim so it must be absolute and
    // same-origin (CWE-601), and the env var may end in trailing slashes.
    //
    // `add_account=1` is appended after: it makes the OAuth callback APPEND to
    // this session instead of replacing it. Without it there is never a second
    // account to switch to.
    const url = new URL(buildOAuthRedirect(provider, "/"));
    url.searchParams.set("add_account", "1");
    window.location.assign(url.toString());
  }, []);

  return {
    accounts,
    addableProviders,
    switchingTo,
    refresh,
    switchTo,
    addAccount,
  };
}
