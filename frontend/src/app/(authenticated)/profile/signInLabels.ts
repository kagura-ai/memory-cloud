/**
 * Sign-in method label helpers for the profile page.
 *
 * Extracted from page.tsx because Next.js 16 App Router rejects arbitrary
 * named exports from a `page.tsx` ("not a valid Page export field"). Keeping
 * these pure i18n helpers in a sibling module lets both the page and its unit
 * tests import them while page.tsx exports only its default component. See #855.
 */

import type { User as AuthUser } from "@/lib/auth/auth";

/**
 * Issue #514: derive the i18n label for the user's sign-in method.
 * Password users always show "Email + Password" regardless of auth_provider.
 * OAuth users with a known provider show that provider's name.
 * Pre-#361 OAuth users may have auth_provider=null; fall back to "Other".
 */
export function getSignInMethodLabel(
  user: Pick<AuthUser, "auth_method" | "auth_provider">,
  t: (key: string) => string,
): string {
  if (user.auth_method === "password") return t("signInMethodPassword");
  if (user.auth_provider === "google") return t("signInMethodGoogle");
  if (user.auth_provider === "github") return t("signInMethodGitHub");
  return t("signInMethodOther");
}

/**
 * Issue #515: localized provider name for i18n message interpolation.
 * Returns null when refresh is not available for the user (password auth
 * or legacy OAuth row with no recorded provider). The brand name itself
 * comes from ``signInMethodGoogle`` / ``signInMethodGitHub`` so all
 * user-visible text — even brand names — flows through next-intl.
 */
export function getRefreshProviderName(
  user: Pick<AuthUser, "auth_method" | "auth_provider">,
  t: (key: string) => string,
): string | null {
  if (user.auth_method !== "oauth") return null;
  if (user.auth_provider === "google") return t("signInMethodGoogle");
  if (user.auth_provider === "github") return t("signInMethodGitHub");
  return null;
}
