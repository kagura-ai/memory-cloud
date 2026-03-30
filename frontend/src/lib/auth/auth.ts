/**
 * Authentication Utilities
 *
 * Handles OAuth2 authentication flows, user information retrieval, and session management.
 */

import { apiClient } from "../api/base";

export interface User {
  id: string;
  email: string;
  name: string;
  picture?: string;
  role?: string;
  timezone?: string; // Issue #175: User timezone
  current_workspace_id?: string;
  current_context_id?: string;
}

export interface AuthResponse {
  user: User;
  token?: string;
}

/**
 * Get the Google OAuth2 authorization URL
 * Redirects to Google OAuth2 consent screen
 */
export async function getAuthUrl(): Promise<string> {
  try {
    const response = await apiClient.get<{ authorization_url: string }>(
      "/api/v1/auth/google/login",
    );
    return response.authorization_url;
  } catch (error) {
    console.error("Failed to get auth URL:", error);
    throw error;
  }
}

/**
 * Get the GitHub OAuth2 authorization URL
 * Issue #315: GitHub OAuth2 Authentication
 */
export async function getGitHubAuthUrl(): Promise<string> {
  try {
    const response = await apiClient.get<{ authorization_url: string }>(
      "/api/v1/auth/github/login",
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
 * Get current authenticated user information
 */
export async function getCurrentUser(): Promise<User | null> {
  try {
    const response = await apiClient.get<{ user: User }>("/api/v1/auth/me");
    return response.user;
  } catch (error) {
    // If 401 Unauthorized, user is not authenticated
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
  const params = returnTo ? `?return_to=${encodeURIComponent(returnTo)}` : "";
  return apiClient.post<PasswordLoginResult>(`/api/v1/auth/login${params}`, {
    login_id: loginId,
    password,
  });
}

/**
 * Verify MFA TOTP code
 */
export async function verifyMfa(
  mfaSessionToken: string,
  totpCode: string,
  returnTo?: string,
): Promise<PasswordLoginResult> {
  const params = returnTo ? `?return_to=${encodeURIComponent(returnTo)}` : "";
  return apiClient.post<PasswordLoginResult>(
    `/api/v1/auth/mfa/verify${params}`,
    { mfa_session_token: mfaSessionToken, totp_code: totpCode },
  );
}
