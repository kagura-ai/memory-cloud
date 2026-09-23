/**
 * Stable test IDs for the beta invite hand-off (#1655): /join/[token] and the
 * /device consent step it returns to. Shared between the components and the
 * Playwright spec so renames are caught at compile time. E2E locators stay
 * locale-independent (testid only, never visible text — #688).
 */
export const INVITE_HANDOFF_TEST_IDS = {
  /** Terms-of-service checkbox on /join/[token]. */
  joinTerms: "join-terms",
  /** "Continue with {provider}" sign-up button on /join/[token]. */
  joinProvider: (provider: string) => `join-continue-${provider}`,
  /** "Approve" on the /device consent screen. */
  deviceApprove: "device-approve",
  /** Heading of the /device success screen. */
  deviceSuccess: "device-success",
} as const;
