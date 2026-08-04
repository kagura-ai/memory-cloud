/**
 * Accounts signed in on THIS browser session (#1488 Phase 3).
 *
 * The server keeps several identities inside one session record and marks one
 * active; these two calls read that list and move the marker. Nothing here can
 * enumerate or reach an account the session does not already hold — the server
 * answers 404 for a user_id that is not a member, so an invented id widens
 * nothing.
 */
import { apiClient } from "./base";

export interface SignedInAccount {
  user_id: string;
  email: string | null;
  name: string | null;
  picture: string | null;
  is_active: boolean;
}

interface AccountsResponse {
  accounts: SignedInAccount[];
}

/** Accounts on this session, active one flagged. */
export async function listAccounts(): Promise<SignedInAccount[]> {
  const res = await apiClient.get<AccountsResponse>("/api/v1/auth/accounts");
  return res.accounts;
}

/**
 * Make an already-signed-in account active.
 *
 * Throws on 404 (not a member of this session) — the caller should treat that
 * as "this account is gone" and refresh the list rather than retrying.
 */
export async function switchAccount(userId: string): Promise<void> {
  await apiClient.post("/api/v1/auth/accounts/switch", { user_id: userId });
}
