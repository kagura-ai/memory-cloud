/**
 * Authentication Utilities
 *
 * Handles OAuth2 authentication flows, user information retrieval, and session management.
 */

import { apiClient } from "../api/base";

/** Build a `?return_to=<encoded>` query string, or "" when returnTo is absent. */
function returnToParam(returnTo?: string): string {
  return returnTo ? `?return_to=${encodeURIComponent(returnTo)}` : "";
}

export interface User {
  id: string;
  email: string;
  name: string;
  picture?: string;
  role?: string;
  timezone?: string; // Issue #175: User timezone
  locale?: string; // Issue #221: User locale
  current_workspace_id?: string;
  current_context_id?: string;
  // Issue #514: sign-in method display
  auth_method?: "password" | "oauth";
  auth_provider?: "google" | "github" | null;
}

export interface AuthResponse {
  user: User;
  token?: string;
}

/**
 * Get the Google OAuth2 authorization URL.
 *
 * When `returnTo` is provided, the backend stores it under the OAuth state
 * (5 min TTL in Redis) and redirects there after the callback. Caller must
 * pre-validate `returnTo` via safeReturnTo (#772) — the value travels through
 * the OAuth round-trip and must already be same-origin safe.
 */
export async function getAuthUrl(returnTo?: string): Promise<string> {
  try {
    const response = await apiClient.get<{ authorization_url: string }>(
      `/api/v1/auth/google/login${returnToParam(returnTo)}`,
    );
    return response.authorization_url;
  } catch (error) {
    console.error("Failed to get auth URL:", error);
    throw error;
  }
}

/**
 * Get the GitHub OAuth2 authorization URL.
 * Issue #315: GitHub OAuth2 Authentication.
 * See `getAuthUrl` for `returnTo` semantics.
 */
export async function getGitHubAuthUrl(returnTo?: string): Promise<string> {
  try {
    const response = await apiClient.get<{ authorization_url: string }>(
      `/api/v1/auth/github/login${returnToParam(returnTo)}`,
    );
    return response.authorization_url;
  } catch (error) {
    console.error("Failed to get GitHub auth URL:", error);
    throw error;
  }
}

/**
 * Handle Google OAuth2 callback
 * Exchange authorization code for session token
 */
export async function handleAuthCallback(
  code: string,
  state: string,
): Promise<AuthResponse> {
  try {
    const response = await apiClient.get<{ user: User; token?: string }>(
      `/api/v1/auth/google/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`,
    );
    return {
      user: response.user,
      token: response.token,
    };
  } catch (error) {
    console.error("OAuth2 callback failed:", error);
    throw error;
  }
}

/**
 * Get current authenticated user information.
 *
 * Public contract (depended on by `AuthContext.refetchUser`, issue #678):
 * - 200 → returns the User
 * - 401 → returns null (caller treats null as "session ended, go to /login")
 * - other errors (5xx, network) → throws (caller preserves current user state)
 *
 * Changing the 401-returns-null branch to throw will silently break the
 * in-session logout path in `AuthContext.refetchUser` — the catch there
 * intentionally preserves the user on errors, so a 401 thrown instead of
 * returned-as-null would leave a logged-out session displayed as logged-in.
 */
export async function getCurrentUser(): Promise<User | null> {
  try {
    const response = await apiClient.get<{ user: User }>("/api/v1/auth/me");
    return response.user;
  } catch (error) {
    if ((error as { status?: number }).status === 401) {
      return null;
    }
    console.error("Failed to get current user:", error);
    throw error;
  }
}

/**
 * Logout current user
 * Clears session on backend
 */
export async function logout(): Promise<void> {
  try {
    await apiClient.post("/api/v1/auth/logout");
  } catch (error) {
    console.error("Logout failed:", error);
    throw error;
  }
}

/**
 * Check if user is authenticated
 * Returns true if session is valid
 */
export async function isAuthenticated(): Promise<boolean> {
  try {
    const user = await getCurrentUser();
    return user !== null;
  } catch {
    return false;
  }
}

// ============================================================================
// Password + MFA Authentication (Issue #51)
// ============================================================================

export interface AuthConfig {
  password_login_enabled: boolean;
  google_oauth_enabled: boolean;
  github_oauth_enabled: boolean;
}

export interface PasswordLoginResult {
  success: boolean;
  mfa_required: boolean;
  mfa_session_token?: string;
  redirect_url?: string;
}

/**
 * Get authentication configuration (which login methods are available)
 */
export async function getAuthConfig(): Promise<AuthConfig> {
  return apiClient.get<AuthConfig>("/api/v1/auth/config");
}

/**
 * Login with username and password
 */
export async function loginWithPassword(
  loginId: string,
  password: string,
  returnTo?: string,
): Promise<PasswordLoginResult> {
  return apiClient.post<PasswordLoginResult>(
    `/api/v1/auth/login${returnToParam(returnTo)}`,
    {
      login_id: loginId,
      password,
    },
  );
}

/**
 * Verify MFA TOTP code
 */
export async function verifyMfa(
  mfaSessionToken: string,
  totpCode: string,
  returnTo?: string,
): Promise<PasswordLoginResult> {
  return apiClient.post<PasswordLoginResult>(
    `/api/v1/auth/mfa/verify${returnToParam(returnTo)}`,
    { mfa_session_token: mfaSessionToken, totp_code: totpCode },
  );
}

// ============================================================================
// Device Authorization Grant (RFC 8628 — Issue #536)
// ============================================================================

export interface DeviceVerifyResponse {
  user_code: string;
  client_name: string;
  scope: string | null;
  expires_at: string;
  is_authorized: boolean;
  is_expired: boolean;
}

export interface DeviceConfirmResponse {
  status: "approved" | "denied";
  user_code: string;
}

/**
 * Verify a device user code and get client info for consent screen.
 */
export async function verifyDeviceCode(
  userCode: string,
): Promise<DeviceVerifyResponse> {
  return apiClient.post<DeviceVerifyResponse>("/api/v1/oauth/device/verify", {
    user_code: userCode,
  });
}

/**
 * Confirm (approve or deny) a device authorization request.
 */
export async function confirmDevice(
  userCode: string,
  approve: boolean,
): Promise<DeviceConfirmResponse> {
  return apiClient.post<DeviceConfirmResponse>("/api/v1/oauth/device/confirm", {
    user_code: userCode,
    approve,
  });
}
