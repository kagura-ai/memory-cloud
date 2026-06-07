"use client";

/**
 * Connected accounts section (Issue #517 — multi-provider OAuth account
 * linking).
 *
 * Lets a signed-in user link / unlink Google and GitHub identities to a single
 * account. Backend contract (Tasks 1-6):
 *   GET  /api/v1/me/account/providers      → { providers: [{provider, ...}] }
 *   POST /api/v1/me/account/link-provider  → { authorization_url, state }
 *   POST /api/v1/me/account/unlink-provider → { status: "ok" } | 409 | 404
 *
 * The "Connect" action performs a full-page browser navigation to the IdP via
 * `window.location.href` (the OAuth flow expects a redirect), matching the
 * canonical pattern in /app/login/page.tsx and the #515 refresh-from-IdP flow
 * — it is NOT an apiClient.get whose JSON is consumed in-page.
 */

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LoadingState } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/contexts/AuthContext";
import { apiClient, ApiError } from "@/lib/api/base";
import { Link2, Loader2 } from "lucide-react";

type Provider = "google" | "github";

const PROVIDERS: readonly Provider[] = ["google", "github"] as const;

interface LinkedProvider {
  provider: string;
  linked_at?: string | null;
  last_used_at?: string | null;
}

interface ProvidersResponse {
  providers: LinkedProvider[];
}

/** Brand glyphs — copied from the canonical login page buttons so the section
 *  is visually consistent. Brand *names* still flow through i18n (t("google")
 *  / t("github")) so all user-visible text is localizable. */
function GoogleGlyph() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 24 24" aria-hidden="true">
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
  );
}

function GitHubGlyph() {
  return (
    <svg
      className="h-5 w-5"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
    </svg>
  );
}

function ProviderGlyph({ provider }: { provider: Provider }) {
  return provider === "google" ? <GoogleGlyph /> : <GitHubGlyph />;
}

export default function ConnectedAccounts() {
  const t = useTranslations("connectedAccounts");
  const tCommon = useTranslations("common");
  const { user } = useAuth();
  const { toast } = useToast();

  const [linked, setLinked] = useState<LinkedProvider[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // The provider whose action (connect or disconnect) is in flight.
  const [busyProvider, setBusyProvider] = useState<Provider | null>(null);
  const [disconnectTarget, setDisconnectTarget] = useState<Provider | null>(
    null,
  );
  const [dialogError, setDialogError] = useState<string | null>(null);

  const loadProviders = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await apiClient.get<ProvidersResponse>(
        "/api/v1/me/account/providers",
      );
      setLinked(data.providers ?? []);
    } catch {
      setLoadError(t("loadError"));
    } finally {
      setIsLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadProviders();
  }, [loadProviders]);

  const linkedSet = new Set(linked.map((p) => p.provider));

  // A password user always retains a fallback sign-in method, so unlinking the
  // last OAuth provider is safe. An OAuth-only user with a single linked
  // provider must keep it — disable Disconnect to pre-empt the backend 409
  // (which is still handled defensively in handleDisconnectConfirm).
  const hasPassword = user?.auth_method === "password";
  const isOnlyMethod = !hasPassword && linkedSet.size <= 1;

  const handleConnect = async (provider: Provider) => {
    setBusyProvider(provider);
    try {
      const data = await apiClient.post<{
        authorization_url: string;
        state: string;
      }>("/api/v1/me/account/link-provider", { provider });
      // Browser-level navigation: the OAuth link flow expects a full redirect.
      window.location.href = data.authorization_url;
    } catch {
      setBusyProvider(null);
      toast({
        title: tCommon("error"),
        description: t("connectError", { provider: t(provider) }),
        variant: "destructive",
      });
    }
  };

  const handleDisconnectConfirm = async () => {
    if (!disconnectTarget) return;
    const provider = disconnectTarget;
    setBusyProvider(provider);
    setDialogError(null);
    try {
      await apiClient.post("/api/v1/me/account/unlink-provider", { provider });
      toast({ title: t("disconnectSuccess", { provider: t(provider) }) });
      setDisconnectTarget(null);
      await loadProviders();
    } catch (error) {
      // 409 = would leave zero auth methods. Surface the API's intent via an
      // Alert inside the still-open dialog (per the error-surface rules:
      // errors raised inside a dialog body use <Alert>, not a toast behind it).
      const messageKey =
        error instanceof ApiError && error.status === 409
          ? "lastMethodError"
          : "disconnectError";
      setDialogError(
        messageKey === "lastMethodError"
          ? t("lastMethodError")
          : t("disconnectError", { provider: t(provider) }),
      );
    } finally {
      setBusyProvider(null);
    }
  };

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Link2 className="h-5 w-5" />
            {t("title")}
          </CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {isLoading ? (
            <LoadingState lines={2} />
          ) : (
            <>
              <ErrorBanner error={loadError} />
              {PROVIDERS.map((provider) => {
                const isLinked = linkedSet.has(provider);
                const isBusy = busyProvider === provider;
                const disableDisconnect = isOnlyMethod || isBusy;
                return (
                  <div
                    key={provider}
                    className="flex items-center justify-between rounded-md border border-slate-200 dark:border-slate-800 p-3"
                  >
                    <div className="flex items-center gap-3">
                      <ProviderGlyph provider={provider} />
                      <div>
                        <p className="text-sm font-medium leading-none">
                          {t(provider)}
                        </p>
                        <p className="text-xs text-slate-500 mt-1">
                          {isLinked ? t("connected") : t("notConnected")}
                        </p>
                      </div>
                    </div>

                    {isLinked ? (
                      <div className="flex flex-col items-end gap-1">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setDialogError(null);
                            setDisconnectTarget(provider);
                          }}
                          disabled={disableDisconnect}
                          aria-label={t("disconnectButton", {
                            provider: t(provider),
                          })}
                        >
                          {isBusy && (
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          )}
                          {t("disconnectButton", { provider: t(provider) })}
                        </Button>
                        {isOnlyMethod && (
                          <p className="text-xs text-slate-500">
                            {t("lastMethodHint")}
                          </p>
                        )}
                      </div>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleConnect(provider)}
                        disabled={isBusy}
                        aria-label={t("connectButton", {
                          provider: t(provider),
                        })}
                      >
                        {isBusy && (
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        )}
                        {isBusy
                          ? t("connecting")
                          : t("connectButton", { provider: t(provider) })}
                      </Button>
                    )}
                  </div>
                );
              })}
            </>
          )}
        </CardContent>
      </Card>

      {/* Disconnect confirmation */}
      <AlertDialog
        open={disconnectTarget !== null}
        onOpenChange={(open) => {
          // Block close while an unlink is in flight so the spinner and the
          // in-dialog error Alert can surface against the open dialog.
          if (!open && busyProvider === null) {
            setDisconnectTarget(null);
            setDialogError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {disconnectTarget
                ? t("disconnectTitle", { provider: t(disconnectTarget) })
                : ""}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {disconnectTarget
                ? t("disconnectDescription", {
                    provider: t(disconnectTarget),
                  })
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {dialogError && (
            <Alert variant="destructive">
              <AlertDescription>{dialogError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busyProvider !== null}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            {/* Regular Button (not AlertDialogAction) so the dialog stays open
                during submission — keeps spinner/disabled visible and lets the
                in-dialog error Alert surface. Closes only on success. */}
            <Button
              variant="destructive"
              onClick={handleDisconnectConfirm}
              disabled={busyProvider !== null}
            >
              {busyProvider !== null && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {busyProvider !== null
                ? t("disconnecting")
                : t("disconnectConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
