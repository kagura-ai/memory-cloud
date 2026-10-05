/**
 * Sign-in method label helpers for the profile page.
 *
 * Extracted from page.tsx because Next.js 16 App Router rejects arbitrary
 * named exports from a `page.tsx` ("not a valid Page export field"). Keeping
 * these pure i18n helpers in a sibling module lets both the page and its unit
 * tests import them while page.tsx exports only its default component. See #855.
 */

import { hasPasswordSignIn, type User as AuthUser } from "@/lib/auth/auth";

/** OAuth providers the profile page can name, in display order. */
const PROVIDER_LABEL_KEYS = {
  google: "signInMethodGoogle",
  github: "signInMethodGitHub",
} as const;

type KnownProvider = keyof typeof PROVIDER_LABEL_KEYS;

function isKnownProvider(provider: string): provider is KnownProvider {
  return Object.prototype.hasOwnProperty.call(PROVIDER_LABEL_KEYS, provider);
}

/**
 * Issue #514 / #1751 / #1884: derive the i18n label for how the user can
 * sign in.
 *
 * - Every linked OAuth provider (Google first), plus "Email and password"
 *   when a password is set, joined by the localized separator.
 * - `auth_method` only records how the account was created, so it does not
 *   pick the label: a password-origin account that later links a provider
 *   names it too, and stops naming the password once it is removed. One
 *   with no linked provider reads "Email and password" alone.
 * - `linkedProviders` is the `/me/account/providers` list; until it loads (or
 *   if it fails) the legacy `auth_provider` pointer stands in for it.
 * - Nothing to name (pre-#361 OAuth row with no provider) → "Other".
 */
export function getSignInMethodLabel(
  user: Pick<AuthUser, "auth_method" | "auth_provider" | "has_password">,
  t: (key: string) => string,
  linkedProviders: readonly string[] | null = null,
): string {
  const providers =
    linkedProviders ?? (user.auth_provider ? [user.auth_provider] : []);
  const labels = (Object.keys(PROVIDER_LABEL_KEYS) as KnownProvider[])
    .filter((provider) => providers.includes(provider))
    .map((provider) => t(PROVIDER_LABEL_KEYS[provider]));
  if (hasPasswordSignIn(user)) labels.push(t("signInMethodEmailPassword"));
  if (labels.length === 0) return t("signInMethodOther");
  return labels.join(t("signInMethodSeparator"));
}

/**
 * Issue #515: localized name of the provider the account signed up with
 * (`auth_provider`), for messages about an IdP refresh. Null for password
 * accounts and legacy OAuth rows with no recorded provider. The brand name
 * itself comes from ``signInMethodGoogle`` / ``signInMethodGitHub`` so all
 * user-visible text — even brand names — flows through next-intl.
 */
export function getProviderName(
  user: Pick<AuthUser, "auth_method" | "auth_provider">,
  t: (key: string) => string,
): string | null {
  if (user.auth_method !== "oauth") return null;
  const provider = user.auth_provider;
  if (!provider || !isKnownProvider(provider)) return null;
  return t(PROVIDER_LABEL_KEYS[provider]);
}

/**
 * Issue #515 / #1751: the provider "Refresh identity" can use, or null to
 * hide it. `POST /me/refresh-oauth` refreshes from `auth_provider`, so it must
 * still be linked. Fails closed: while the linked providers are loading, or if
 * they could not be read (`linkedProviders === null`), there is no refresh.
 */
export function getRefreshProviderName(
  user: Pick<AuthUser, "auth_method" | "auth_provider">,
  t: (key: string) => string,
  linkedProviders: readonly string[] | null,
): string | null {
  if (!linkedProviders || !user.auth_provider) return null;
  if (!linkedProviders.includes(user.auth_provider)) return null;
  return getProviderName(user, t);
}
