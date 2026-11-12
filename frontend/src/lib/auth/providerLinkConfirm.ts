/**
 * Provider link confirmation client (#2786).
 *
 * A Google or GitHub sign-in that used the address of an account created by
 * email sign-up can be connected to that account by an emailed single-use
 * link (`/link/confirm?token=…`). The page posts the token from the link in a
 * JSON body. The shared client sends cookies: `complete` connects only in the
 * browser that started the sign-in, which the backend recognises by an
 * HttpOnly cookie this code never reads.
 */

import { apiClient } from "@/lib/api/base";

export type LinkProvider = "google" | "github";

export interface ProviderLinkPreview {
  provider: LinkProvider;
  /** GitHub login or Google address: the primary identifier. */
  provider_login: string;
  /** Free text from the provider (secondary), or null. */
  provider_name: string | null;
  provider_avatar_url: string | null;
  /** ISO 8601 with offset. */
  expires_at: string;
  /** Only true where the sign-in was started: only there can it complete. */
  same_browser: boolean;
}

export interface ProviderLinkCompleted {
  /** Same-origin, validated by the backend. */
  redirect_url: string;
}

const BASE = "/api/v1/auth/link-confirm";

/** The provider account a live link connects. 400 when it is not live. */
export async function previewProviderLink(
  token: string,
): Promise<ProviderLinkPreview> {
  return apiClient.post<ProviderLinkPreview>(`${BASE}/preview`, { token });
}

/**
 * Connect the provider account and sign this browser in. 400 = not live,
 * 403 = another browser, 503 = connected but no session (sign in again).
 */
export async function completeProviderLink(
  token: string,
): Promise<ProviderLinkCompleted> {
  return apiClient.post<ProviderLinkCompleted>(`${BASE}/complete`, { token });
}

/** "This wasn't me": discard the link and stop further offers. Always 204. */
export async function cancelProviderLink(token: string): Promise<void> {
  await apiClient.post(`${BASE}/cancel`, { token });
}

const AVATAR_HOSTS: Record<LinkProvider, string> = {
  github: "avatars.githubusercontent.com",
  google: "lh3.googleusercontent.com",
};

/**
 * Whether an avatar URL may be shown: https on the provider's own avatar host,
 * nothing else. The backend applies the same rule; this keeps the page from
 * loading anything else if that ever changes.
 */
export function isAllowedProviderAvatar(
  provider: LinkProvider,
  url: string | null | undefined,
): url is string {
  if (!url) return false;
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return (
    parsed.protocol === "https:" &&
    parsed.hostname === AVATAR_HOSTS[provider] &&
    parsed.port === "" &&
    parsed.username === "" &&
    parsed.password === ""
  );
}
