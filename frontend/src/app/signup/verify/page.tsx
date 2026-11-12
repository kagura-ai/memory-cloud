"use client";

/**
 * Email sign-up link landing page (#1734): /signup/verify?token=…
 *
 * The second step of the email + password sign-up. Whoever opens the link
 * proved they read the mailbox, so this is where the password is chosen (a
 * password typed on the first form would let anyone pre-register someone
 * else's address). The address is shown prominently, so a person sent
 * someone else's link sees whose account they are about to create.
 *
 * The token is read once into state and the URL is replaced with the bare
 * path (PasswordLinkPage pattern); it is sent to the backend only and never
 * logged or stored. The preview consumes nothing, so a mail scanner opening
 * the link does not spend it.
 *
 * The return path comes from this browser (`peekSignupReturnTo`), never from
 * the link: opened elsewhere, the new account lands on the dashboard.
 *
 * Outcomes of `complete`: 400 the link is not usable, 403 the sign-up
 * controls refuse it now, 409 the address has an account by now, 422 the
 * password policy or the terms (field-adjacent), 429 a toast, 503 the account
 * was created but no session opened (sign in).
 */

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Info,
  UserPlus,
  type LucideIcon,
} from "lucide-react";

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
import { SpinnerLoading } from "@/components/common/LoadingState";
import { NewPasswordForm } from "@/components/auth/NewPasswordForm";
import { TermsAgreement } from "@/components/auth/TermsAgreement";
import { useSystemInfo } from "@/hooks/useSystemFeatures";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/base";
import {
  clearSignupReturnTo,
  completeEmailSignup,
  peekSignupReturnTo,
  previewEmailSignup,
} from "@/lib/auth/emailSignup";

type Phase =
  | "loading"
  | "form"
  | "invalid"
  | "notAllowed"
  | "exists"
  | "created"
  | "error";

function Shell({
  icon: Icon,
  iconClass,
  title,
  description,
  children,
}: {
  icon: LucideIcon;
  iconClass?: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-slate-50 to-slate-100 px-4 dark:from-slate-900 dark:to-slate-800">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Icon className={`h-5 w-5 ${iconClass ?? ""}`} aria-hidden="true" />
            {title}
          </CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">{children}</CardContent>
      </Card>
    </div>
  );
}

function SignupVerify() {
  const t = useTranslations("emailSignup");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  // #1665: the terms version the checkbox refers to. The submit waits for the
  // first /system/info answer; a failed fetch resolves to "no version".
  const systemInfo = useSystemInfo();
  const systemInfoPending = systemInfo === null;
  const termsVersion = systemInfo?.terms_version ?? undefined;

  // Read once: the URL loses the token right below.
  const [token] = useState<string | null>(() => searchParams.get("token"));
  useEffect(() => {
    if (token) router.replace(pathname, { scroll: false });
  }, [token, router, pathname]);

  const [phase, setPhase] = useState<Phase>(token ? "loading" : "invalid");
  const [email, setEmail] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [name, setName] = useState("");
  const [agreedToTerms, setAgreedToTerms] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [policyError, setPolicyError] = useState(false);
  const [termsError, setTermsError] = useState(false);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    previewEmailSignup(token)
      .then((preview) => {
        if (!alive) return;
        setEmail(preview.email);
        setPhase("form");
      })
      .catch((err: unknown) => {
        if (!alive) return;
        // Deliberately not logged: the request body carries the token.
        const status = err instanceof ApiError ? err.status : 0;
        // 404: the deployment turned email sign-up off.
        setPhase(status === 400 || status === 404 ? "invalid" : "error");
      });
    return () => {
      alive = false;
    };
  }, [token, attempt]);

  const handleSubmit = async (newPassword: string) => {
    if (!token || systemInfoPending) return;
    if (termsVersion !== undefined && !agreedToTerms) return;
    setSubmitting(true);
    setPolicyError(false);
    setTermsError(false);
    try {
      const { redirect_url } = await completeEmailSignup({
        token,
        newPassword,
        name: name.trim() || undefined,
        acceptedTerms: termsVersion,
        returnTo: peekSignupReturnTo(window.location.origin),
      });
      clearSignupReturnTo();
      // A full navigation: the new session cookie must reach every provider.
      window.location.href = redirect_url;
    } catch (err) {
      setSubmitting(false);
      const apiError = err instanceof ApiError ? err : null;
      const status = apiError?.status ?? 0;
      if (status === 400) setPhase("invalid");
      else if (status === 403) setPhase("notAllowed");
      else if (status === 409) setPhase("exists");
      else if (status === 503) {
        clearSignupReturnTo();
        setPhase("created");
      } else if (status === 422) {
        if (apiError?.details?.field === "accepted_terms") setTermsError(true);
        else setPolicyError(true);
      } else
        toast({
          title: tCommon("error"),
          description:
            status === 429 ? t("verify.tooManyRequests") : t("verify.genericError"),
          variant: "destructive",
        });
    }
  };

  const backToLogin = (
    <p className="text-center text-sm">
      <Link href="/login" className="text-muted-foreground underline">
        {t("verify.backToLogin")}
      </Link>
    </p>
  );

  if (phase === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <SpinnerLoading message={t("verify.loading")} />
      </div>
    );
  }

  if (phase === "invalid") {
    return (
      <Shell
        icon={AlertTriangle}
        iconClass="text-red-800 dark:text-red-300"
        title={t("verify.invalidTitle")}
        description={t("verify.invalidMessage")}
      >
        {backToLogin}
      </Shell>
    );
  }

  if (phase === "notAllowed") {
    return (
      <Shell
        icon={AlertTriangle}
        iconClass="text-amber-700 dark:text-amber-300"
        title={t("verify.notAllowedTitle")}
        description={t("verify.notAllowedMessage")}
      >
        {backToLogin}
      </Shell>
    );
  }

  if (phase === "exists") {
    return (
      <Shell
        icon={Info}
        title={t("verify.existsTitle")}
        description={t("verify.existsMessage")}
      >
        <Button asChild className="w-full">
          <Link href="/login">{t("verify.signIn")}</Link>
        </Button>
        <Button asChild variant="outline" className="w-full">
          <Link href="/password/forgot">{t("verify.forgotPassword")}</Link>
        </Button>
      </Shell>
    );
  }

  if (phase === "created") {
    return (
      <Shell
        icon={CheckCircle2}
        iconClass="text-green-600 dark:text-green-400"
        title={t("verify.createdTitle")}
        description={t("verify.createdMessage")}
      >
        <Button asChild className="w-full">
          <Link href="/login">{t("verify.signIn")}</Link>
        </Button>
      </Shell>
    );
  }

  if (phase === "error") {
    return (
      <Shell
        icon={AlertCircle}
        iconClass="text-amber-700 dark:text-amber-300"
        title={t("verify.errorTitle")}
        description={t("verify.errorMessage")}
      >
        <Button
          className="w-full"
          onClick={() => {
            setPhase("loading");
            setAttempt((n) => n + 1);
          }}
        >
          {t("verify.retry")}
        </Button>
        {backToLogin}
      </Shell>
    );
  }

  return (
    <Shell
      icon={UserPlus}
      title={t("verify.title")}
      description={t("verify.description", { email })}
    >
      <div className="space-y-2">
        <Label htmlFor="signup-name">{t("verify.nameLabel")}</Label>
        <Input
          id="signup-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          autoComplete="name"
          maxLength={255}
          disabled={submitting}
        />
      </div>
      <NewPasswordForm
        submitLabel={t("verify.submit")}
        submittingLabel={t("verify.submitting")}
        submitting={submitting}
        policyError={policyError}
        submitDisabled={
          systemInfoPending || (termsVersion !== undefined && !agreedToTerms)
        }
        onSubmit={(pw) => void handleSubmit(pw)}
      >
        {termsVersion !== undefined && (
          <div className="space-y-2">
            <TermsAgreement
              checked={agreedToTerms}
              onCheckedChange={(checked) => {
                setAgreedToTerms(checked);
                setTermsError(false);
              }}
              themed
            />
            {/* #2761 announcement opt-in */}
          </div>
        )}
        {termsError && (
          <p role="alert" className="text-sm text-red-700 dark:text-red-300">
            {t("verify.termsRequired")}
          </p>
        )}
      </NewPasswordForm>
      {backToLogin}
    </Shell>
  );
}

export default function SignupVerifyPage() {
  return (
    <Suspense fallback={<SpinnerLoading size="lg" />}>
      <SignupVerify />
    </Suspense>
  );
}
