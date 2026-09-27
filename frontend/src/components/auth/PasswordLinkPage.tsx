"use client";

/**
 * Landing page for an emailed password link (Issue #1678).
 *
 * `mode="reset"`  — /password/reset?token=…  (forgot-password link)
 * `mode="setup"`  — /password/setup?token=…  (set-a-password link from the profile)
 *
 * The token rides the URL (unavoidable for an email link); it is sent once to
 * the backend and never logged or stored. A reset signs the account out
 * everywhere, so success points at /login; a set-up keeps this browser's
 * session, so success points at the profile.
 */

import Link from "next/link";
import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { AlertTriangle, CheckCircle2, KeyRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { NewPasswordForm } from "@/components/auth/NewPasswordForm";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/base";
import { resetPassword, setupPassword } from "@/lib/auth/auth";

type Phase = "form" | "success" | "invalid";

interface PasswordLinkPageProps {
  mode: "reset" | "setup";
}

export function PasswordLinkPage({ mode }: PasswordLinkPageProps) {
  const t = useTranslations("passwordRecovery");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const searchParams = useSearchParams();
  const token = searchParams.get("token");

  const [phase, setPhase] = useState<Phase>(token ? "form" : "invalid");
  const [submitting, setSubmitting] = useState(false);
  const [policyError, setPolicyError] = useState(false);

  const handleSubmit = async (newPassword: string) => {
    if (!token) return;
    setSubmitting(true);
    setPolicyError(false);
    try {
      if (mode === "reset") {
        await resetPassword(token, newPassword);
      } else {
        await setupPassword(token, newPassword);
      }
      setPhase("success");
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      if (status === 400) setPhase("invalid");
      else if (status === 422) setPolicyError(true);
      else
        toast({
          title: tCommon("error"),
          description:
            status === 429 ? t("tooManyRequests") : t("genericError"),
          variant: "destructive",
        });
    } finally {
      setSubmitting(false);
    }
  };

  let title: string;
  let description: string;
  if (phase === "success") {
    title =
      mode === "reset" ? t("reset.successTitle") : t("setup.successTitle");
    description =
      mode === "reset" ? t("reset.successBody") : t("setup.successBody");
  } else if (phase === "invalid") {
    title =
      mode === "reset" ? t("reset.invalidTitle") : t("setup.invalidTitle");
    description =
      mode === "reset" ? t("reset.invalidBody") : t("setup.invalidBody");
  } else {
    title = mode === "reset" ? t("reset.title") : t("setup.title");
    description =
      mode === "reset" ? t("reset.description") : t("setup.description");
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100 px-4 dark:from-slate-900 dark:to-slate-800">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {phase === "success" ? (
              <CheckCircle2 className="h-5 w-5 text-green-600 dark:text-green-400" />
            ) : phase === "invalid" ? (
              <AlertTriangle className="h-5 w-5 text-red-800 dark:text-red-300" />
            ) : (
              <KeyRound className="h-5 w-5" />
            )}
            {title}
          </CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {phase === "form" && (
            <NewPasswordForm
              submitLabel={
                mode === "reset" ? t("reset.submit") : t("setup.submit")
              }
              submittingLabel={
                mode === "reset" ? t("reset.submitting") : t("setup.submitting")
              }
              submitting={submitting}
              policyError={policyError}
              onSubmit={(pw) => void handleSubmit(pw)}
            />
          )}

          {phase === "success" &&
            (mode === "reset" ? (
              <Button asChild className="w-full">
                <Link href="/login">{t("reset.signIn")}</Link>
              </Button>
            ) : (
              <Button asChild className="w-full">
                <Link href="/profile">{t("setup.continue")}</Link>
              </Button>
            ))}

          {phase === "invalid" &&
            (mode === "reset" ? (
              <Button asChild variant="outline" className="w-full">
                <Link href="/password/forgot">{t("reset.requestNew")}</Link>
              </Button>
            ) : (
              <Button asChild variant="outline" className="w-full">
                <Link href="/profile">{t("setup.continue")}</Link>
              </Button>
            ))}

          {phase !== "success" && (
            <p className="text-center text-sm">
              <Link href="/login" className="text-muted-foreground underline">
                {t("backToLogin")}
              </Link>
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
