/**
 * Expiry selection → `expires_days` request value (#1537).
 *
 * The server treats an omitted/null `expires_days` as "use the deployment
 * default" (365 days), so "Never" must be sent as an explicit `0` — the only
 * way a key can end up without an expiry is by asking for it.
 */

export const DEFAULT_EXPIRY_SELECTION = "365";
export const NEVER_EXPIRY_SELECTION = "never";

export function expiresDaysFromSelection(selection: string): number {
  if (selection === NEVER_EXPIRY_SELECTION) return 0;
  return parseInt(selection, 10);
}
