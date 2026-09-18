/**
 * Expiry selection → `expires_days` request value (#1537).
 *
 * The server treats an omitted/null `expires_days` as "use the deployment
 * default" (`API_KEY_DEFAULT_EXPIRES_DAYS`, 365 days unless the operator
 * changed it), so the dialog's default selection maps to `null` rather than
 * a literal 365 — otherwise a tightened deployment default would be silently
 * bypassed from the UI. "Never" must be sent as an explicit `0` — the only
 * way a key can end up without an expiry is by asking for it.
 */

export const SERVER_DEFAULT_EXPIRY_SELECTION = "default";
export const NEVER_EXPIRY_SELECTION = "never";
export const DEFAULT_EXPIRY_SELECTION = SERVER_DEFAULT_EXPIRY_SELECTION;

export function expiresDaysFromSelection(selection: string): number | null {
  if (selection === SERVER_DEFAULT_EXPIRY_SELECTION) return null;
  if (selection === NEVER_EXPIRY_SELECTION) return 0;
  return parseInt(selection, 10);
}
