"use client";

/**
 * Unsubscribe from announcement emails (#2761).
 *
 * The link in an announcement email opens `/email/unsubscribe#<token>`. The
 * token rides in the fragment, which no server sees; the page removes it from
 * the address bar at once and posts it in a request body when the person
 * presses the button (never on load: link scanners run pages). No sign-in is
 * needed and a session, if any, plays no part: the token decides.
 *
 * The backend answers the same for every token, so the page shows one "done"
 * message whatever the outcome; it never says whose setting changed. Only a
 * rate limit or a failure to reach the server shows an error.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { MailCheck, MailX } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import {
  InlineSpinner,
  SpinnerLoading,
} from "@/components/common/LoadingState";
import { ApiError } from "@/lib/api/base";
import { unsubscribeAnnouncementEmails } from "@/lib/api/announcementEmails";

type Phase = "reading" | "ready" | "missing" | "working" | "done";

export default function UnsubscribePage() {
  const t = useTranslations("announcementEmails.unsubscribe");
  const [token, setToken] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("reading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const value = window.location.hash.replace(/^#/, "");
    if (window.location.hash) {
      // Out of the address bar and the history entry.
      window.history.replaceState(
        null,
        "",
        window.location.pathname + window.location.search,
      );
    }
    // The fragment exists only in the browser, so it is read after mount.
    /* eslint-disable react-hooks/set-state-in-effect */
    setToken(value || null);
    setPhase(value ? "ready" : "missing");
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  const handleUnsubscribe = async () => {
    if (!token) return;
    setPhase("working");
    setError(null);
    try {
      await unsubscribeAnnouncementEmails(token);
      setToken(null);
      setPhase("done");
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      setError(status === 429 ? t("rateLimited") : t("error"));
      setPhase("ready");
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100 px-4 dark:from-slate-900 dark:to-slate-800">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {phase === "done" ? (
              <MailCheck className="h-5 w-5" />
            ) : (
              <MailX className="h-5 w-5" />
            )}
            {phase === "done" ? t("doneTitle") : t("title")}
          </CardTitle>
          {phase !== "done" && (
            <CardDescription>{t("message")}</CardDescription>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {phase === "reading" && <SpinnerLoading />}
          {phase === "missing" && (
            <Alert>
              <AlertDescription>{t("invalidLink")}</AlertDescription>
            </Alert>
          )}
          {phase === "done" && (
            <Alert data-testid="unsubscribe-done">
              <MailCheck className="h-4 w-4" />
              <AlertDescription>{t("done")}</AlertDescription>
            </Alert>
          )}
          <ErrorBanner error={error} />
          {(phase === "ready" || phase === "working") && (
            <Button
              className="w-full"
              onClick={() => void handleUnsubscribe()}
              disabled={phase === "working"}
              data-testid="unsubscribe-button"
            >
              {phase === "working" && <InlineSpinner className="mr-2" />}
              {phase === "working" ? t("working") : t("button")}
            </Button>
          )}
          <p className="text-center text-sm">
            <Link
              href="/profile#profile-emails"
              className="text-muted-foreground underline"
            >
              {t("manage")}
            </Link>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
