/**
 * The sign-in page's language and the browser's time zone, sent with every
 * OAuth login (#2700).
 *
 * The backend binds them to the OAuth state and applies them only when that
 * sign-in creates the account; an existing account keeps its own settings.
 * Without them a new account starts as `en` / `UTC`, and the authenticated
 * layout — which treats `user.locale` as the source of truth — switches
 * someone who signed up in Japanese to English right after sign-up.
 */
import { defaultLocale, getStoredLocale } from "@/i18n/config";

export interface SignupPrefs {
  /** UI language, from the sign-in page's language selector. */
  locale: string;
  /** IANA time zone (e.g. `Asia/Tokyo`); absent when the browser has none. */
  timezone?: string;
}

/** `users.timezone` width; the backend rejects anything longer. */
const MAX_TIMEZONE_LENGTH = 50;

export function signupPrefs(): SignupPrefs {
  let locale: string = defaultLocale;
  try {
    locale = getStoredLocale();
  } catch {
    // Storage blocked: the default language, as the page itself falls back.
  }

  let timezone: unknown;
  try {
    timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    // No time zone support: send none, and the account keeps UTC.
  }
  if (
    typeof timezone === "string" &&
    timezone !== "" &&
    timezone.length <= MAX_TIMEZONE_LENGTH
  ) {
    return { locale, timezone };
  }
  return { locale };
}
