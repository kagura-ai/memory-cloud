"use client";

/**
 * Login Page
 *
 * Supports password + MFA login and optional Google/GitHub OAuth.
 * Issue #51: Password + MFA login for initial admin.
 * Issue #223: i18n support.
 * Issue #315: GitHub OAuth2.
 * Issue #360: Provider discovery.
 */

import { useEffect, useState, Suspense } from "react";
import { useTranslations } from "next-intl";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  getAuthUrl,
  getGitHubAuthUrl,
  getAuthConfig,
  loginWithPassword,
  verifyMfa,
  type AuthConfig,
} from "@/lib/auth/auth";
import { ArrowRight, AlertCircle, Sparkles, Shield, Zap } from "lucide-react";
import { KaguraLogo } from "@/components/icons/KaguraLogo";
import { LanguageSelector } from "@/components/LanguageSelector";

function LoginContent() {
  const t = useTranslations("login");
  const router = useRouter();
  const searchParams = useSearchParams();

  const [loadingAction, setLoadingAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [agreedToTerms, setAgreedToTerms] = useState(false);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [showAdminLogin, setShowAdminLogin] = useState<boolean | null>(null);

  // Password login state
  const [loginId, setLoginId] = useState("");
  const [password, setPassword] = useState("");

  // MFA state
  const [mfaRequired, setMfaRequired] = useState(false);
  const [mfaSessionToken, setMfaSessionToken] = useState("");
  const [totpCode, setTotpCode] = useState("");

  const returnTo = searchParams.get("return_to") ?? undefined;

  const isMockAuth =
    process.env.NODE_ENV === "development" &&
    process.env.NEXT_PUBLIC_ENABLE_MOCK_AUTH === "true";

  useEffect(() => {
    const errorParam = searchParams.get("error");
    if (errorParam === "registration_disabled") {
      setError(
        t("registrationDisabled", {
          default:
            "Registration is disabled. Please ask an admin for an invitation.",
        }),
      );
    } else if (errorParam) {
      setError(decodeURIComponent(errorParam));
    }

    if (isMockAuth) {
      router.push("/workspace/contexts");
      return;
    }

    getAuthConfig()
      .then((config) => {
        setAuthConfig(config);
        // Auto-show admin login if no OAuth providers configured
        const hasOAuth = config.google_oauth_enabled || config.github_oauth_enabled;
        if (!hasOAuth && config.password_login_enabled) {
          setShowAdminLogin(true);
        } else {
          setShowAdminLogin(false);
        }
      })
      .catch(() => {
        setAuthConfig({
          password_login_enabled: true,
          google_oauth_enabled: false,
          github_oauth_enabled: false,
        });
        setShowAdminLogin(true);
      });
  }, [searchParams, isMockAuth, router, t]);

  const handlePasswordLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoadingAction("password");
    setError(null);

    try {
      const result = await loginWithPassword(loginId, password, returnTo);

      if (result.mfa_required && result.mfa_session_token) {
        setMfaRequired(true);
        setMfaSessionToken(result.mfa_session_token);
        setLoadingAction(null);
        return;
      }

      if (result.redirect_url) {
        window.location.href = result.redirect_url;
      } else {
        router.push("/workspace/dashboard");
      }
    } catch {
      setLoadingAction(null);
      setError(t("invalidCredentials"));
    }
  };

  const handleMfaVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoadingAction("mfa");
    setError(null);

    try {
      const result = await verifyMfa(mfaSessionToken, totpCode, returnTo);

      if (result.redirect_url) {
        window.location.href = result.redirect_url;
      } else {
        router.push("/workspace/dashboard");
      }
    } catch {
      setLoadingAction(null);
      setError(t("invalidCredentials"));
    }
  };

  const handleGoogleLogin = async () => {
    setLoadingAction("google");
    setError(null);
    try {
      const authUrl = await getAuthUrl();
      window.location.href = authUrl;
    } catch (err) {
      setLoadingAction(null);
      setError(err instanceof Error ? err.message : t("failedToLogin"));
    }
  };

  const handleGitHubLogin = async () => {
    setLoadingAction("github");
    setError(null);
    try {
      const authUrl = await getGitHubAuthUrl();
      window.location.href = authUrl;
    } catch (err) {
      setLoadingAction(null);
      setError(err instanceof Error ? err.message : t("failedToLogin"));
    }
  };

  const hasOAuth =
    authConfig?.google_oauth_enabled || authConfig?.github_oauth_enabled;

  if (isMockAuth) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-white">
        <div className="text-center">
          <div className="relative mx-auto mb-4">
            <div className="h-16 w-16 animate-spin rounded-full border-4 border-brand-green-200 border-t-brand-green-600" />
          </div>
          <p className="text-lg font-semibold text-gray-700">
            {t("mockAuthEnabled")}
          </p>
          <p className="text-sm text-gray-500 mt-2">
            {t("redirectingToDashboard")}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-white">
      {/* Background */}
      <div className="absolute inset-0 -z-10 bg-[linear-gradient(to_right,#8080800a_1px,transparent_1px),linear-gradient(to_bottom,#8080800a_1px,transparent_1px)] bg-[size:14px_24px]" />
      <div className="absolute inset-0 -z-10 bg-gradient-to-b from-white via-gray-50/50 to-white" />
      <div className="pointer-events-none absolute -left-1/4 -top-1/4 h-96 w-96 rounded-full bg-brand-green-300/30 blur-3xl" />
      <div className="pointer-events-none absolute -right-1/4 -bottom-1/4 h-96 w-96 rounded-full bg-emerald-300/30 blur-3xl" />

      <div className="absolute top-4 right-4 z-10">
        <LanguageSelector className="!bg-white/90 backdrop-blur-sm border border-gray-300 shadow-sm hover:!bg-white !text-gray-700 hover:!text-gray-900" showLabel />
      </div>

      <div className="relative w-full max-w-md px-4">
        <div className="mb-8 flex justify-center">
          <KaguraLogo className="h-24 w-auto" />
        </div>

        <Card className="overflow-hidden border-gray-200 bg-white/80 shadow-2xl backdrop-blur-xl">
          <CardContent className="p-8">
            {/* Badge */}
            <div className="mb-6 flex justify-center">
              <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-brand-green-100 to-emerald-100 px-4 py-1.5 text-sm font-semibold text-brand-green-700">
                <Sparkles className="h-4 w-4" />
                <span>{t("welcomeToKagura")}</span>
              </div>
            </div>

            {/* Title */}
            <div className="mb-8 text-center">
              <h1 className="mb-2 text-3xl font-bold text-gray-900">
                {mfaRequired ? t("mfaRequired") : t("signInToAccount")}
              </h1>
              <p className="text-gray-600">
                {mfaRequired ? t("enterTotpCode") : t("accessPlatform")}
              </p>
            </div>

            {/* Error */}
            {error && (
              <Alert
                variant="destructive"
                className="mb-6 border-red-200 bg-red-50"
              >
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            {mfaRequired ? (
              /* MFA Form */
              <form onSubmit={handleMfaVerify} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="totpCode" className="text-gray-700">
                    {t("totpCode")}
                  </Label>
                  <Input
                    id="totpCode"
                    type="text"
                    inputMode="numeric"
                    pattern="[0-9]*"
                    maxLength={6}
                    value={totpCode}
                    onChange={(e) => setTotpCode(e.target.value)}
                    placeholder="000000"
                    className="bg-white text-gray-900 text-center text-2xl tracking-widest"
                    autoFocus
                    autoComplete="one-time-code"
                  />
                </div>
                <Button
                  type="submit"
                  disabled={loadingAction !== null || totpCode.length !== 6}
                  className="h-12 w-full bg-gradient-to-r from-brand-green-600 to-emerald-600 text-base font-semibold text-white shadow-lg hover:from-brand-green-700 hover:to-emerald-700 disabled:opacity-50"
                >
                  {loadingAction === "mfa" ? t("verifying") : t("verify")}
                </Button>
              </form>
            ) : (
              <>
                {/* Admin Password Login Form (hidden by default) */}
                {showAdminLogin && authConfig?.password_login_enabled && (
                  <>
                    <form onSubmit={handlePasswordLogin} className="space-y-4">
                      <div className="space-y-2">
                        <Label htmlFor="loginId" className="text-gray-700">
                          {t("loginId")}
                        </Label>
                        <Input
                          id="loginId"
                          type="text"
                          value={loginId}
                          onChange={(e) => setLoginId(e.target.value)}
                          autoFocus
                          autoComplete="username"
                          className="bg-white text-gray-900"
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="password" className="text-gray-700">
                          {t("password")}
                        </Label>
                        <Input
                          id="password"
                          type="password"
                          value={password}
                          onChange={(e) => setPassword(e.target.value)}
                          autoComplete="current-password"
                          className="bg-white text-gray-900"
                        />
                      </div>

                      {/* Terms */}
                      <div>
                        <label className="flex items-start gap-3 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={agreedToTerms}
                            onChange={(e) => setAgreedToTerms(e.target.checked)}
                            className="mt-1 h-4 w-4 rounded border-gray-300 text-brand-green-600 focus:ring-brand-green-500"
                          />
                          <span className="text-sm text-gray-700">
                            {t("agreeToTerms")}{" "}
                            <a
                              href="/terms"
                              target="_blank"
                              className="font-medium text-brand-green-600 hover:underline"
                            >
                              {t("termsOfService")}
                            </a>{" "}
                            {t("termsAndPrivacy")}{" "}
                            <a
                              href="/privacy"
                              target="_blank"
                              className="font-medium text-brand-green-600 hover:underline"
                            >
                              {t("privacyPolicy")}
                            </a>
                          </span>
                        </label>
                      </div>

                      <Button
                        type="submit"
                        disabled={
                          loadingAction !== null ||
                          !agreedToTerms ||
                          !loginId ||
                          !password
                        }
                        className="h-12 w-full bg-gradient-to-r from-brand-green-600 to-emerald-600 text-base font-semibold text-white shadow-lg hover:from-brand-green-700 hover:to-emerald-700 disabled:opacity-50"
                      >
                        {loadingAction === "password"
                          ? t("signingIn")
                          : t("signIn")}
                      </Button>
                    </form>

                    {/* Divider between admin form and OAuth */}
                    {hasOAuth && (
                      <div className="my-6 flex items-center gap-3">
                        <div className="h-px flex-1 bg-gray-200" />
                        <span className="text-sm text-gray-500">
                          {t("orContinueWith")}
                        </span>
                        <div className="h-px flex-1 bg-gray-200" />
                      </div>
                    )}
                  </>
                )}

                {/* Google */}
                {authConfig?.google_oauth_enabled && (
                  <Button
                    onClick={handleGoogleLogin}
                    disabled={loadingAction !== null || !agreedToTerms}
                    size="lg"
                    variant={showAdminLogin ? "outline" : "default"}
                    className={`group relative h-14 w-full overflow-hidden text-base font-semibold transition-all hover:scale-[1.02] disabled:opacity-50 disabled:hover:scale-100 ${
                      !showAdminLogin
                        ? "bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white shadow-xl shadow-brand-green-500/30 hover:from-brand-green-700 hover:to-emerald-700"
                        : "shadow-md hover:shadow-lg"
                    }`}
                  >
                    {loadingAction === "google" ? (
                      <span className="flex items-center justify-center gap-2">
                        <div className="h-5 w-5 animate-spin rounded-full border-2 border-current border-t-transparent" />
                        {t("redirectingToGoogle")}
                      </span>
                    ) : (
                      <span className="flex items-center justify-center gap-2">
                        <svg className="h-5 w-5" viewBox="0 0 24 24">
                          <path
                            fill="#4285F4"
                            d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
                          />
                          <path
                            fill="#34A853"
                            d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
                          />
                          <path
                            fill="#FBBC05"
                            d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"
                          />
                          <path
                            fill="#EA4335"
                            d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
                          />
                        </svg>
                        {t("continueWithGoogle")}
                        <ArrowRight className="ml-1 h-5 w-5 transition-transform group-hover:translate-x-1" />
                      </span>
                    )}
                  </Button>
                )}

                {/* GitHub */}
                {authConfig?.github_oauth_enabled && (
                  <>
                    {authConfig.google_oauth_enabled && !showAdminLogin && (
                      <div className="relative my-2">
                        <div className="absolute inset-0 flex items-center">
                          <span className="w-full border-t border-gray-300" />
                        </div>
                        <div className="relative flex justify-center text-xs uppercase">
                          <span className="bg-white px-2 text-gray-500">
                            or
                          </span>
                        </div>
                      </div>
                    )}
                    <Button
                      onClick={handleGitHubLogin}
                      disabled={loadingAction !== null || !agreedToTerms}
                      size="lg"
                      variant="outline"
                      className="group relative mt-2 h-14 w-full overflow-hidden text-base font-semibold shadow-md transition-all hover:scale-[1.02] hover:shadow-lg disabled:opacity-50 disabled:hover:scale-100"
                    >
                      {loadingAction === "github" ? (
                        <span className="flex items-center justify-center gap-2">
                          <div className="h-5 w-5 animate-spin rounded-full border-2 border-gray-400 border-t-transparent" />
                          {t("redirecting", { default: "Redirecting..." })}
                        </span>
                      ) : (
                        <span className="flex items-center justify-center gap-2">
                          <svg
                            className="h-5 w-5"
                            viewBox="0 0 24 24"
                            fill="currentColor"
                          >
                            <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
                          </svg>
                          {t("continueWithGitHub", {
                            default: "Continue with GitHub",
                          })}
                          <ArrowRight className="ml-1 h-5 w-5 transition-transform group-hover:translate-x-1" />
                        </span>
                      )}
                    </Button>
                  </>
                )}

                {/* Terms (shown here when admin form is hidden) */}
                {!showAdminLogin && (
                  <div className="mt-6">
                    <label className="flex items-start gap-3 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={agreedToTerms}
                        onChange={(e) => setAgreedToTerms(e.target.checked)}
                        className="mt-1 h-4 w-4 rounded border-gray-300 text-brand-green-600 focus:ring-brand-green-500"
                      />
                      <span className="text-sm text-gray-700">
                        {t("agreeToTerms")}{" "}
                        <a
                          href="/terms"
                          target="_blank"
                          className="font-medium text-brand-green-600 hover:underline"
                        >
                          {t("termsOfService")}
                        </a>{" "}
                        {t("termsAndPrivacy")}{" "}
                        <a
                          href="/privacy"
                          target="_blank"
                          className="font-medium text-brand-green-600 hover:underline"
                        >
                          {t("privacyPolicy")}
                        </a>
                      </span>
                    </label>
                  </div>
                )}
              </>
            )}

            {/* Features */}
            {!mfaRequired && (
              <div className="mt-8 space-y-3">
                {[
                  { icon: Shield, text: t("secureOAuth") },
                  { icon: Zap, text: t("instantAccess") },
                  { icon: Sparkles, text: t("freeForever") },
                ].map((feature) => {
                  const Icon = feature.icon;
                  return (
                    <div
                      key={feature.text}
                      className="flex items-center gap-3 text-sm text-gray-700"
                    >
                      <div className="flex-shrink-0 rounded-lg bg-brand-green-100 p-2 text-brand-green-600">
                        <Icon className="h-4 w-4" />
                      </div>
                      <span>{feature.text}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        <div className="mt-6 flex items-center justify-center gap-4">
          {authConfig && (() => {
            const hasOAuth = authConfig.google_oauth_enabled || authConfig.github_oauth_enabled;
            if (!hasOAuth || showAdminLogin) return null;
            return (
              <button
                onClick={() => setShowAdminLogin(true)}
                className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white/60 px-4 py-2 text-sm font-medium text-gray-600 backdrop-blur-sm transition-colors hover:bg-white hover:text-brand-green-600"
              >
                <Shield className="h-4 w-4" />
                {t("adminLogin")}
              </button>
            );
          })()}
          <button
            onClick={() => router.push("/")}
            className="text-sm font-medium text-gray-500 transition-colors hover:text-brand-green-600"
          >
            {t("backToHome")}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center bg-white">
          <div className="relative">
            <div className="h-16 w-16 animate-spin rounded-full border-4 border-brand-green-200 border-t-brand-green-600" />
            <div className="absolute inset-0 h-16 w-16 animate-ping rounded-full border-4 border-brand-green-600 opacity-20" />
          </div>
        </div>
      }
    >
      <LoginContent />
    </Suspense>
  );
}
