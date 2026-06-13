"use client";

/**
 * Account erasure confirmation landing page (Issue #953).
 *
 * This is the URL the backend emails to OAuth users:
 *   {frontend_url}/account/erasure/confirm?token=…
 * (see backend account_erasure_service.py — the confirm link target).
 *
 * It lives OUTSIDE the (authenticated) route group on purpose. The shared
 * authenticated layout redirects unauthenticated users to /login WITHOUT a
 * return_to (it would dead-end the email link: confirm requires the session
 * cookie to match the request's user_id, and the token is single-use + 1h).
 * So we self-gate here: if the user isn't logged in, bounce to
 * /login?return_to=<this URL incl. token> — safeReturnTo preserves the query
 * and login forwards return_to through the OAuth round-trip, so they come back
 * here authenticated and the confirm fires.
 *
 * The token is sensitive — it rides the URL (unavoidable for an email link)
 * but is never logged.
 */

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { AlertTriangle, CheckCircle2 } from "lucide-react";

import { useAuth } from "@/contexts/AuthContext";
import { formatDate } from "@/lib/utils/datetime";
import { confirmErasure } from "@/lib/api/account-erasure";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { SpinnerLoading } from "@/components/common/LoadingState";

type Phase = "loading" | "success" | "invalid";

// sessionStorage key for the one-shot login-bounce guard (see the effect).
const REDIRECT_FLAG = "erasure_confirm_redirected";

function ConfirmErasureInner() {
  const t = useTranslations("accountDeletion");
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, isLoading, isAuthenticated } = useAuth();

  const token = searchParams.get("token");
  const [phase, setPhase] = useState<Phase>("loading");
  const [scheduledFor, setScheduledFor] = useState<string | null>(null);
  const ran = useRef(false);

  useEffect(() => {
    if (!token) {
      setPhase("invalid");
      return;
    }
    // Wait for the initial /auth/me to settle before deciding.
    if (isLoading) return;

    if (!isAuthenticated) {
      // One-shot guard against an infinite bounce: if we already went through
      // /login and came back still unauthenticated (session cookie not readable
      // post-OAuth — Safari ITP, third-party-cookie blocking, cookie-domain
      // mismatch), don't redirect again — show the invalid state instead of
      // looping and burning the single-use token's TTL.
      if (sessionStorage.getItem(REDIRECT_FLAG)) {
        sessionStorage.removeItem(REDIRECT_FLAG);
        setPhase("invalid");
        return;
      }
      sessionStorage.setItem(REDIRECT_FLAG, "1");
      // Bounce through login, preserving the full confirm URL (incl. token).
      const returnTo = `/account/erasure/confirm?token=${encodeURIComponent(token)}`;
      router.replace(`/login?return_to=${encodeURIComponent(returnTo)}`);
      return;
    }

    // Authenticated: clear the bounce marker and confirm exactly once
    // (StrictMode double-mount guard).
    sessionStorage.removeItem(REDIRECT_FLAG);
    if (ran.current) return;
    ran.current = true;

    confirmErasure(token)
      .then((state) => {
        setScheduledFor(state.scheduled_for);
        setPhase("success");
      })
      .catch(() => setPhase("invalid"));
  }, [token, isLoading, isAuthenticated, router]);

  if (phase === "loading") {
    return <SpinnerLoading size="lg" message={t("confirmPageLoading")} />;
  }

  const scheduledLabel =
    scheduledFor && formatDate(scheduledFor, user?.timezone ?? "UTC", locale);

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {phase === "success" ? (
              <>
                <CheckCircle2 className="h-5 w-5 text-green-600 dark:text-green-400" />
                {t("confirmPageSuccessTitle")}
              </>
            ) : (
              <>
                <AlertTriangle className="h-5 w-5 text-red-800 dark:text-red-300" />
                {t("confirmPageInvalidTitle")}
              </>
            )}
          </CardTitle>
          <CardDescription>
            {phase === "success"
              ? t("confirmPageSuccessBody", { date: scheduledLabel ?? "" })
              : t("confirmPageInvalidBody")}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button onClick={() => router.push("/profile")}>
            {t("goToProfile")}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}

export default function ConfirmErasurePage() {
  return (
    <Suspense fallback={<SpinnerLoading size="lg" />}>
      <ConfirmErasureInner />
    </Suspense>
  );
}
