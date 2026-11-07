/**
 * The UI language and the browser's time zone, sent with every OAuth login
 * (#2700).
 *
 * The backend binds them to the OAuth state and applies them only when that
 * sign-in creates the account; an existing account keeps its own settings.
 * Without them a new account starts as `en` / `UTC`, and the authenticated
 * layout — which treats `user.locale` as the source of truth — switches
 * someone who signed up in Japanese to English right after sign-up.
 *
 * On the sign-in, join and invite pages the language is the one their
 * language selector shows. On "add another account" (`startAddAccount`) it is
 * the signed-in account's current UI language: the same person is adding an
 * account, so a new one starting in that language is intended.
 */
import { defaultLocale, getStoredLocale } from "@/i18n/config";
import { browserTimezone } from "@/lib/utils/datetime";

export interface SignupPrefs {
  /** UI language (`getStoredLocale()`). */
  locale: string;
  /** IANA time zone (e.g. `Asia/Tokyo`); absent when there is none usable. */
  timezone?: string;
}

export function signupPrefs(): SignupPrefs {
  let locale: string = defaultLocale;
  try {
    locale = getStoredLocale();
  } catch {
    // Storage blocked: the default language, as the page itself falls back.
  }
  // Without one the account keeps UTC.
  const timezone = browserTimezone();
  return timezone ? { locale, timezone } : { locale };
}
