/**
 * Email + password sign-up client (#1734).
 *
 * Three calls, verify first:
 * 1. `requestEmailSignup` — the backend always answers 202 with the same
 *    body, whether or not a link is sent, so the caller shows one neutral
 *    message ("if this address can be used, we sent a link").
 * 2. `previewEmailSignup` — the address and expiry of a live link
 *    (`/signup/verify`). Consumes nothing.
 * 3. `completeEmailSignup` — the link holder chooses the password, accepts
 *    the terms and the account is created; the response carries the session
 *    cookie and where to go next.
 *
 * The return path (e.g. `/invite/<token>` or `/device?user_code=…`) is never
 * sent with the request: it stays in the browser that started the sign-up
 * (`rememberSignupReturnTo`) and travels with `complete`, where the backend
 * validates it again. A link opened in another browser lands on the
 * dashboard, so a mailed link can never carry a destination someone else
 * picked.
 *
 * The emailed token is a credential: callers never log or persist it.
 */

import { apiClient } from "@/lib/api/base";
import { safeReturnTo } from "@/lib/auth/safeReturnTo";
import { signupPrefs } from "@/lib/auth/signupPrefs";

const BASE = "/api/v1/auth/signup/email";

export interface RequestEmailSignupInput {
  email: string;
  /** A beta invite token (`/join/<token>`); the backend ignores a bad one. */
  invite?: string;
}

/** Ask for a sign-up link. Resolves the same way for every address. */
export async function requestEmailSignup(
  input: RequestEmailSignupInput,
): Promise<void> {
  const body: Record<string, unknown> = { email: input.email };
  if (input.invite) body.invite = input.invite;
  // #2700: the new account starts in the page's language and time zone.
  Object.assign(body, signupPrefs());
  await apiClient.post(`${BASE}/request`, body);
}

export interface EmailSignupPreview {
  email: string;
  expires_at: string;
}

/** The address a live link was sent to. 400 = invalid, expired or used. */
export async function previewEmailSignup(
  token: string,
): Promise<EmailSignupPreview> {
  return apiClient.post<EmailSignupPreview>(`${BASE}/preview`, { token });
}

export interface CompleteEmailSignupInput {
  token: string;
  newPassword: string;
  name?: string;
  /** The terms version the page showed (#1665), when the deployment has one. */
  acceptedTerms?: string;
  /** The path kept by `rememberSignupReturnTo`; validated again server-side. */
  returnTo?: string;
  /**
   * The announcement-email opt-in (#2761), ticked or not, with the
   * `announcement_consent_version` the page showed; absent when no box was shown.
   */
  announcementEmails?: { value: boolean; version: string };
}

/**
 * Create the account and sign this browser in.
 * 400 link not usable, 403 the sign-up controls refuse it now, 409 the
 * address has an account by now, 422 password policy / terms, 503 the
 * account was created but no session could be opened.
 */
export async function completeEmailSignup(
  input: CompleteEmailSignupInput,
): Promise<{ redirect_url: string }> {
  const body: Record<string, unknown> = {
    token: input.token,
    new_password: input.newPassword,
  };
  if (input.name) body.name = input.name;
  if (input.acceptedTerms) body.accepted_terms = input.acceptedTerms;
  if (input.returnTo) body.return_to = input.returnTo;
  if (input.announcementEmails) {
    body.announcement_emails = input.announcementEmails.value;
    body.announcement_consent_version = input.announcementEmails.version;
  }
  return apiClient.post<{ redirect_url: string }>(`${BASE}/complete`, body);
}

// ── The return path, kept in the browser that started the sign-up ─────────

export const SIGNUP_RETURN_TO_KEY = "kagura.emailSignup.returnTo";
// Longer than any link lives (EMAIL_SIGNUP_LINK_TTL_MINUTES caps at 24 h).
const RETURN_TO_TTL_MS = 24 * 60 * 60 * 1000;

function currentOrigin(): string {
  return typeof window !== "undefined" ? window.location.origin : "";
}

/** Remove the kept return path. Never throws (storage may be blocked). */
export function clearSignupReturnTo(): void {
  try {
    window.localStorage.removeItem(SIGNUP_RETURN_TO_KEY);
  } catch {
    // Storage blocked: nothing was kept either.
  }
}

/**
 * Keep where to go after the sign-up completes. An empty or unsafe value
 * clears what an older sign-up kept, so it can never apply to this one.
 */
export function rememberSignupReturnTo(path: string | undefined): void {
  const safe = safeReturnTo(path, currentOrigin());
  if (!safe) {
    clearSignupReturnTo();
    return;
  }
  try {
    window.localStorage.setItem(
      SIGNUP_RETURN_TO_KEY,
      JSON.stringify({ path: safe, expiresAt: Date.now() + RETURN_TO_TTL_MS }),
    );
  } catch {
    // Storage blocked: the sign-up lands on the dashboard.
  }
}

/** The kept return path, if it is still fresh and safe; never throws. */
export function peekSignupReturnTo(origin: string): string | undefined {
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(SIGNUP_RETURN_TO_KEY);
  } catch {
    return undefined;
  }
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as { path?: unknown; expiresAt?: unknown };
    if (
      typeof parsed?.path !== "string" ||
      typeof parsed.expiresAt !== "number" ||
      Date.now() > parsed.expiresAt
    ) {
      return undefined;
    }
    return safeReturnTo(parsed.path, origin);
  } catch {
    return undefined;
  }
}
