/**
 * Stable test IDs for the ConnectedAccounts section (#517 UI, #937 E2E).
 *
 * Shared between the component and the Playwright spec so renames are caught at
 * compile time. Per #688 gate1 (QA Lead): E2E locators must be locale-independent
 * (testid only, never visible text) so specs survive the next-intl migration.
 */
export const CONNECTED_ACCOUNTS_TEST_IDS = {
  /** "Connect {provider}" button (shown when the provider is NOT linked). */
  connect: (provider: string) => `connected-accounts-connect-${provider}`,
  /** "Disconnect {provider}" button (shown when the provider IS linked). */
  disconnect: (provider: string) => `connected-accounts-disconnect-${provider}`,
  /** Per-provider status line ("Connected" / "Not connected"). */
  status: (provider: string) => `connected-accounts-status-${provider}`,
  /** Destructive confirm button inside the disconnect AlertDialog. */
  disconnectConfirm: "connected-accounts-disconnect-confirm",
} as const;
