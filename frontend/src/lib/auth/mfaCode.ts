/** Normalize the value for validation and submission without interrupting IME composition. */
export function normalizeMfaCode(raw: string): string {
  return raw.normalize("NFKC").replace(/\s/g, "");
}

export function isCompleteMfaCode(code: string, allowRecovery: boolean): boolean {
  return /^\d{6}$/.test(code) ||
    (allowRecovery && /^(?:[\da-f]{8}-){3}[\da-f]{8}$/i.test(code));
}
