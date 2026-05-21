"use client";

/**
 * Accept Invitation Page
 *
 * Issue #165: Team Collaboration Features - Workspace Invitation System
 * UX Improvements: Better auth flow and error messaging
 *
 * Flow:
 * 1. Load invitation info (public)
 * 2. Check authentication status
 * 3. Show appropriate screen:
 *    - Not authenticated → Login prompt
 *    - Authenticated + email mismatch → Logout prompt
 *    - Authenticated + email match → Auto-accept
 *
 * Next.js 15: params is now a Promise and must be unwrapped with React.use()
 */

import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  acceptInvitation,
  getInvitationInfo,
  AcceptInvitationResponse,
  InvitationInfo,
} from "@/lib/api/invitations";
import { apiClient, ApiError } from "@/lib/api/base";
import {
  buildOAuthRedirect,
  type OAuthProvider,
} from "@/lib/auth/buildOAuthRedirect";
import { Button } from "@/components/ui/button";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { LanguageSelector } from "@/components/LanguageSelector";
import { Check, AlertCircle, Github, LogIn, Mail } from "lucide-react";

type PageState =
  | "loading"
  | "login_required"
  | "email_mismatch"
  | "accepting"
  | "success"
  | "error";

interface CurrentUser {
  user_id: string;
  email: string;
  name: string;
}

export default function AcceptInvitationPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const router = useRouter();
  const { token } = use(params);
  const t = useTranslations("invitation");

  const [state, setState] = useState<PageState>("loading");
  const [invitationInfo, setInvitationInfo] = useState<InvitationInfo | null>(
    null,
  );
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [result, setResult] = useState<AcceptInvitationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    initializeAcceptanceFlow();
  }, [token]);

  const initializeAcceptanceFlow = async () => {
    try {
      setState("loading");

      // Step 1: Load invitation info (public endpoint)
      const info = await getInvitationInfo(token);
      setInvitationInfo(info);

      // Step 2: Check authentication
      const user = await checkAuthentication();

      if (!user) {
        // Not authenticated → Show login prompt
        setState("login_required");
        return;
      }

      setCurrentUser(user);

      // Step 3: Check email match (if invitation has email restriction)
      if (info.email_restricted) {
        // Note: Backend will validate, but we check here for better UX
        // We don't have the actual email in invitationInfo for privacy,
        // so we'll attempt accept and handle error
      }

      // Step 4: Attempt to accept invitation
      await attemptAcceptInvitation(user);
    } catch (err: unknown) {
      console.error("Initialization error:", err);

      let errorMessage = "Failed to load invitation";
      if (err instanceof ApiError) {
        if (err.status === 404) {
          errorMessage = "Invitation not found or invalid";
        } else if (err.status === 410) {
          errorMessage =
            err.details?.detail ||
            err.message ||
            "This invitation has expired or been accepted";
        } else if (err.details?.detail) {
          errorMessage = err.details.detail;
        } else {
          errorMessage = err.message;
        }
      } else if (err instanceof Error) {
        errorMessage = err.message;
      }

      setError(errorMessage);
      setState("error");
    }
  };

  const checkAuthentication = async (): Promise<CurrentUser | null> => {
    try {
      const user = await apiClient.get<CurrentUser>("/api/v1/auth/me");
      return user;
    } catch {
      return null; // Not authenticated
    }
  };

  const attemptAcceptInvitation = async (user: CurrentUser) => {
    try {
      setState("accepting");
      const response = await acceptInvitation(token);
      setResult(response);
      setState("success");

      // Redirect after 3 seconds
      setTimeout(() => {
        router.push("/workspace/members");
      }, 3000);
    } catch (err: unknown) {
      console.error("[INVITE] ❌ Acceptance failed:", err);

      const apiError = err instanceof ApiError ? err : null;
      const errorMessage =
        apiError?.details?.detail ||
        (err instanceof Error ? err.message : "Failed to accept invitation");

      // Check if it's an email mismatch error
      if (
        errorMessage.includes("restricted to") &&
        errorMessage.includes("logged in as")
      ) {
        setState("email_mismatch");
        setError(errorMessage);
      } else if (errorMessage.includes("already a member")) {
        // Already a member - treat as success
        router.push("/workspace/members");
      } else {
        setState("error");
        setError(errorMessage);
      }
    }
  };

  const startOAuthLogin = (provider: OAuthProvider) => {
    const returnTo = window.location.pathname + window.location.search;
    window.location.href = buildOAuthRedirect(provider, returnTo);
  };

  const handleLogout = async () => {
    try {
      // Call logout API (POST)
      await apiClient.post("/api/v1/auth/logout", {});
      // Reload page to re-check auth status
      window.location.reload();
    } catch (err) {
      console.error("Logout failed:", err);
      // Force reload anyway
      window.location.reload();
    }
  };

  const maskEmail = (email: string): string => {
    if (!email || !email.includes("@")) return email;

    const [localPart, domain] = email.split("@");

    // Mask local part: show first 2 chars + *** + last char
    let maskedLocal = localPart;
    if (localPart.length > 3) {
      maskedLocal =
        localPart.substring(0, 2) +
        "***" +
        localPart.substring(localPart.length - 1);
    } else if (localPart.length > 1) {
      maskedLocal = localPart[0] + "***";
    }

    // Mask domain: show first char + *** + TLD
    const domainParts = domain.split(".");
    if (domainParts.length > 1) {
      const baseDomain = domainParts[0];
      const tld = domainParts.slice(1).join(".");
      const maskedDomain = baseDomain[0] + "***." + tld;
      return `${maskedLocal}@${maskedDomain}`;
    }

    return `${maskedLocal}@${domain}`;
  };

  // Loading State
  if (state === "loading" || state === "accepting") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
        <div className="absolute top-4 right-4">
          <LanguageSelector />
        </div>
        <div className="text-center">
          <SpinnerLoading
            message={
              state === "loading" ? t("status.loading") : t("status.accepting")
            }
          />
        </div>
      </div>
    );
  }

  // Login Required State
  if (state === "login_required") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-4">
        <div className="absolute top-4 right-4">
          <LanguageSelector />
        </div>
        <div className="max-w-md w-full">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
            <div className="flex items-center justify-center w-16 h-16 bg-blue-100 dark:bg-blue-900/20 rounded-full mx-auto mb-4">
              <LogIn className="w-8 h-8 text-blue-600 dark:text-blue-400" />
            </div>

            <h2 className="text-2xl font-bold text-center text-gray-900 dark:text-gray-100 mb-2">
              {t("loginRequired.title")}
            </h2>

            <p className="text-center text-gray-600 dark:text-gray-400 mb-6">
              {t("loginRequired.message")}
              {invitationInfo && (
                <>
                  {" "}
                  {t("loginRequired.messageTo")}{" "}
                  <strong className="text-gray-900 dark:text-gray-100">
                    {invitationInfo.workspace_name}
                  </strong>
                </>
              )}
            </p>

            <Button
              onClick={() => startOAuthLogin("google")}
              size="lg"
              className="w-full mb-3 text-base [&_svg]:size-5"
            >
              <LogIn />
              {t("loginRequired.loginButton")}
            </Button>

            <Button
              onClick={() => startOAuthLogin("github")}
              variant="outline"
              size="lg"
              className="w-full mb-4 text-base [&_svg]:size-5"
            >
              <Github />
              {t("loginRequired.continueWithGitHub")}
            </Button>

            <p className="text-xs text-center text-gray-500 dark:text-gray-400">
              {t("loginRequired.noAccount")}
            </p>
          </div>
        </div>
      </div>
    );
  }

  // Email Mismatch State
  if (state === "email_mismatch") {
    // Extract emails from error message (capture full email with @domain)
    const restrictedMatch = error?.match(
      /restricted to ([^\s]+@[^\s.]+\.[^\s]+)/,
    );
    const loggedInMatch = error?.match(/logged in as ([^\s]+@[^\s.]+\.[^\s]+)/);
    const requiredEmail = restrictedMatch?.[1] || "the invited email";
    const userEmail = loggedInMatch?.[1] || currentUser?.email || "unknown";

    // Mask emails for privacy
    const maskedRequired = maskEmail(requiredEmail);
    const maskedCurrent = maskEmail(userEmail);

    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-4">
        <div className="absolute top-4 right-4">
          <LanguageSelector />
        </div>
        <div className="max-w-md w-full">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
            <div className="flex items-center justify-center w-16 h-16 bg-amber-100 dark:bg-amber-900/20 rounded-full mx-auto mb-4">
              <AlertCircle className="w-8 h-8 text-amber-600 dark:text-amber-400" />
            </div>

            <h2 className="text-2xl font-bold text-center text-gray-900 dark:text-gray-100 mb-2">
              {t("emailMismatch.title")}
            </h2>

            <div className="space-y-4 mb-6">
              <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
                <p className="text-sm text-blue-900 dark:text-blue-100 mb-2 font-medium">
                  {t("emailMismatch.required")}
                </p>
                <div className="flex items-center gap-2 text-blue-700 dark:text-blue-300">
                  <Mail className="w-4 h-4" />
                  <span className="font-mono font-semibold">
                    {maskedRequired}
                  </span>
                </div>
              </div>

              <div className="bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
                <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">
                  {t("emailMismatch.currentlyLoggedIn")}
                </p>
                <div className="flex items-center gap-2 text-gray-600 dark:text-gray-400">
                  <Mail className="w-4 h-4" />
                  <span className="font-mono">{maskedCurrent}</span>
                </div>
              </div>
            </div>

            <Button
              onClick={handleLogout}
              size="lg"
              className="w-full mb-4 text-base [&_svg]:size-5"
            >
              <LogIn />
              {t("emailMismatch.logoutButton")}
            </Button>

            <p className="text-xs text-center text-gray-500 dark:text-gray-400">
              {t("emailMismatch.returnNote")}
            </p>
          </div>
        </div>
      </div>
    );
  }

  // Success State
  if (state === "success" && result) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-4">
        <div className="absolute top-4 right-4">
          <LanguageSelector />
        </div>
        <div className="max-w-md w-full">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
            <div className="flex items-center justify-center w-16 h-16 bg-green-100 dark:bg-green-900/20 rounded-full mx-auto mb-4">
              <Check className="w-8 h-8 text-green-600 dark:text-green-400" />
            </div>

            <h2 className="text-2xl font-bold text-center text-gray-900 dark:text-gray-100 mb-2">
              {t("success.title", { workspaceName: result.workspace.name })}
            </h2>

            <p className="text-center text-gray-600 dark:text-gray-400 mb-6">
              {t("success.message")}{" "}
              <strong className="text-blue-600 dark:text-blue-400">
                {t(`roles.${result.member.role.toLowerCase()}`)}
              </strong>
            </p>

            <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4 mb-6">
              <p className="text-sm text-blue-800 dark:text-blue-200">
                {t("success.accessNote")}
              </p>
            </div>

            <div className="text-center">
              <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
                {t("success.redirecting")}
              </p>

              <button
                onClick={() => router.push("/workspace/members")}
                className="w-full px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors"
              >
                {t("success.goToWorkspace")}
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Error State
  if (state === "error") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-4">
        <div className="absolute top-4 right-4">
          <LanguageSelector />
        </div>
        <div className="max-w-md w-full">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
            <div className="flex items-center justify-center w-16 h-16 bg-red-100 dark:bg-red-900/20 rounded-full mx-auto mb-4">
              <AlertCircle className="w-8 h-8 text-red-600 dark:text-red-400" />
            </div>

            <h2 className="text-2xl font-bold text-center text-gray-900 dark:text-gray-100 mb-2">
              {t("error.title")}
            </h2>

            <p className="text-center text-red-600 dark:text-red-400 mb-6">
              {error}
            </p>

            <div className="space-y-2 text-sm text-gray-600 dark:text-gray-400 mb-6">
              <p>{t("error.mayHave")}</p>
              <ul className="list-disc list-inside space-y-1 ml-4">
                <li>{t("error.alreadyAccepted")}</li>
                <li>{t("error.expired")}</li>
                <li>{t("error.revoked")}</li>
                <li>{t("error.invalidToken")}</li>
              </ul>
            </div>

            <Button
              onClick={() => router.push("/workspace/dashboard")}
              variant="secondary"
              className="w-full"
            >
              {t("error.goToDashboard")}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return null;
}
