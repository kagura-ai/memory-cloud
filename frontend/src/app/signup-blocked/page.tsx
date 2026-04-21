"use client";

/**
 * Signup Blocked Page (Issue #358)
 *
 * The OAuth callback redirects here when SignupGateService.check_access
 * blocks a registration attempt. Deliberately simple — Phase 1 focuses on
 * correctness, not polish. Copy invites users to contact the administrator.
 */

import Link from "next/link";
import { AlertCircle, Lock } from "lucide-react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";

export default function SignupBlockedPage() {
  const t = useTranslations("signupBlocked");

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100 dark:from-slate-900 dark:to-slate-800 px-4">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Lock className="h-5 w-5 text-red-600" />
            {t("title")}
          </CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{t("message")}</AlertDescription>
          </Alert>

          <p className="text-sm text-muted-foreground">{t("contact")}</p>

          <div className="pt-2">
            <Link href="/login" className="w-full">
              <Button variant="outline" className="w-full">
                {t("backToLogin")}
              </Button>
            </Link>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
