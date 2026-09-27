"use client";

/**
 * Forgot-password page (#1678).
 *
 * Asks for the account email and always answers with the same neutral message
 * — the backend does the same — so the page never reveals whether an account
 * exists. Only a rate-limit refusal or a network failure shows an error (a
 * destructive toast: it is a failed user action).
 */

import Link from "next/link";
import { useState } from "react";
import { useTranslations } from "next-intl";
import { KeyRound, MailCheck } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineSpinner } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/base";
import { requestPasswordReset } from "@/lib/auth/auth";

export default function ForgotPasswordPage() {
  const t = useTranslations("passwordRecovery");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await requestPasswordReset(email.trim());
      setSent(true);
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      toast({
        title: tCommon("error"),
        description: status === 429 ? t("tooManyRequests") : t("genericError"),
        variant: "destructive",
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100 px-4 dark:from-slate-900 dark:to-slate-800">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-5 w-5" />
            {t("forgot.title")}
          </CardTitle>
          <CardDescription>{t("forgot.description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {sent ? (
            <Alert>
              <MailCheck className="h-4 w-4" />
              <AlertDescription>{t("forgot.sent")}</AlertDescription>
            </Alert>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">{t("forgot.emailLabel")}</Label>
                <Input
                  id="email"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email"
                  autoFocus
                  required
                />
              </div>
              <Button
                type="submit"
                className="w-full"
                disabled={submitting || !email.trim()}
              >
                {submitting && <InlineSpinner className="mr-2" />}
                {submitting ? t("forgot.submitting") : t("forgot.submit")}
              </Button>
            </form>
          )}
          <p className="text-center text-sm">
            <Link href="/login" className="text-muted-foreground underline">
              {t("backToLogin")}
            </Link>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
