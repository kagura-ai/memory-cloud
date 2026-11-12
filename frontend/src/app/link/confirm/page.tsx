"use client";

/**
 * Provider link confirmation page (#2786): /link/confirm?token=…
 *
 * A Google or GitHub sign-in that used the address of an account created by
 * email sign-up was answered `email_in_use`, and the account's address was
 * mailed this link. Opened in the browser that started that sign-in, it
 * connects the provider account to the email account and signs in. Opened
 * anywhere else it connects nothing: the page then offers no "Connect"
 * button, only "This wasn't me".
 *
 * The page names the provider account (login first, display name second) and
 * says what connecting it means, so the owner of the mailbox decides with the
 * facts in front of them. "This wasn't me" discards the link and stops
 * further offers for the account.
 *
 * The token is read once into state and the URL is replaced with the bare
 * path (PasswordLinkPage pattern); it is sent to the backend only and never
 * logged or stored. The preview consumes nothing, so a mail scanner opening
 * the link does not spend it.
 */

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Link2,
  MonitorSmartphone,
  ShieldCheck,
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
import { SpinnerLoading } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/base";
import {
  cancelProviderLink,
  completeProviderLink,
  isAllowedProviderAvatar,
  previewProviderLink,
  type ProviderLinkPreview,
} from "@/lib/auth/providerLinkConfirm";

type Phase =
  | "loading"
  | "confirm"
  | "otherBrowser"
  | "invalid"
  | "rateLimited"
  | "cancelled"
  | "connectedSignInAgain"
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
  description?: string;
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
          {description && <CardDescription>{description}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">{children}</CardContent>
      </Card>
    </div>
  );
}

/** The provider account being connected: avatar or initial, login, name. */
function ProviderAccount({
  preview,
  label,
}: {
  preview: ProviderLinkPreview;
  label: string;
}) {
  const avatar = isAllowedProviderAvatar(
    preview.provider,
    preview.provider_avatar_url,
  )
    ? preview.provider_avatar_url
    : null;
  const initial = preview.provider_login.charAt(0).toUpperCase();
  return (
    <div className="flex items-center gap-3 rounded-md border border-border bg-muted/40 p-3">
      {avatar ? (
        // A fixed allowlisted host; next/image would need a remote pattern
        // and adds nothing for one 40px picture.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={avatar}
          alt=""
          referrerPolicy="no-referrer"
          className="h-10 w-10 rounded-full"
        />
      ) : (
        <div
          aria-hidden="true"
          className="flex h-10 w-10 items-center justify-center rounded-full bg-muted text-base font-semibold text-muted-foreground"
        >
          {initial}
        </div>
      )}
      <div className="min-w-0">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="break-all font-semibold text-foreground">
          {preview.provider_login}
        </p>
        {preview.provider_name && (
          <p className="break-words text-sm text-muted-foreground">
            {preview.provider_name}
          </p>
        )}
      </div>
    </div>
  );
}

function ProviderLinkConfirm() {
  const t = useTranslations("providerLinkConfirm");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  // Read once: the URL loses the token right below.
  const [token] = useState<string | null>(() => searchParams.get("token"));
  useEffect(() => {
    if (token) router.replace(pathname, { scroll: false });
  }, [token, router, pathname]);

  const [phase, setPhase] = useState<Phase>(token ? "loading" : "invalid");
  const [preview, setPreview] = useState<ProviderLinkPreview | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState<"connect" | "cancel" | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    previewProviderLink(token)
      .then((result) => {
        if (!alive) return;
        setPreview(result);
        setPhase(result.same_browser ? "confirm" : "otherBrowser");
      })
      .catch((err: unknown) => {
        if (!alive) return;
        // Deliberately not logged: the request body carries the token.
        const status = err instanceof ApiError ? err.status : 0;
        // 404: the deployment turned the confirmation off. 422: a malformed
        // token. Both read as an unusable link.
        if (status === 400 || status === 404 || status === 422)
          setPhase("invalid");
        else if (status === 429) setPhase("rateLimited");
        else setPhase("error");
      });
    return () => {
      alive = false;
    };
  }, [token, attempt]);

  const failWith = (status: number) => {
    if (status === 400 || status === 404 || status === 422) setPhase("invalid");
    else if (status === 429) setPhase("rateLimited");
    else
      toast({
        title: tCommon("error"),
        description: t("genericError"),
        variant: "destructive",
      });
  };

  const handleConnect = async () => {
    if (!token || busy) return;
    setBusy("connect");
    try {
      const { redirect_url } = await completeProviderLink(token);
      // A full navigation: the new session cookie must reach every provider.
      // The backend validated the target (it may be a sign-in the person
      // started from an MCP client).
      window.location.href = redirect_url;
    } catch (err) {
      setBusy(null);
      const status = err instanceof ApiError ? err.status : 0;
      // 403: this is not the browser that started the sign-in.
      if (status === 403) setPhase("otherBrowser");
      // 503: connected, but no session could be opened here.
      else if (status === 503) setPhase("connectedSignInAgain");
      else failWith(status);
    }
  };

  const handleCancel = async () => {
    if (!token || busy) return;
    setBusy("cancel");
    try {
      await cancelProviderLink(token);
      setPhase("cancelled");
    } catch (err) {
      failWith(err instanceof ApiError ? err.status : 0);
    } finally {
      setBusy(null);
    }
  };

  const provider = preview
    ? preview.provider === "github"
      ? t("providerGithub")
      : t("providerGoogle")
    : "";

  const backToLogin = (
    <p className="text-center text-sm">
      <Link href="/login" className="text-muted-foreground underline">
        {t("backToLogin")}
      </Link>
    </p>
  );

  const notMeButton = (
    <Button
      variant="outline"
      className="w-full"
      disabled={busy !== null}
      onClick={() => void handleCancel()}
    >
      {t("notMe")}
    </Button>
  );

  if (phase === "loading") {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <SpinnerLoading message={t("loading")} />
      </div>
    );
  }

  if (phase === "invalid") {
    return (
      <Shell
        icon={AlertTriangle}
        iconClass="text-red-800 dark:text-red-300"
        title={t("invalidTitle")}
        description={t("invalidMessage")}
      >
        {backToLogin}
      </Shell>
    );
  }

  if (phase === "rateLimited") {
    return (
      <Shell
        icon={Clock}
        iconClass="text-amber-700 dark:text-amber-300"
        title={t("rateLimitedTitle")}
        description={t("rateLimitedMessage")}
      >
        {backToLogin}
      </Shell>
    );
  }

  if (phase === "error" || !preview) {
    return (
      <Shell
        icon={AlertCircle}
        iconClass="text-amber-700 dark:text-amber-300"
        title={t("errorTitle")}
        description={t("errorMessage")}
      >
        <Button
          className="w-full"
          onClick={() => {
            setPhase("loading");
            setAttempt((n) => n + 1);
          }}
        >
          {t("retry")}
        </Button>
        {backToLogin}
      </Shell>
    );
  }

  if (phase === "cancelled") {
    return (
      <Shell
        icon={ShieldCheck}
        iconClass="text-green-600 dark:text-green-400"
        title={t("cancelledTitle")}
        description={t("cancelledMessage")}
      >
        <p className="text-sm text-muted-foreground">
          {t("alwaysWorks", { provider })}
        </p>
        <Button asChild className="w-full">
          <Link href="/login">{t("signIn")}</Link>
        </Button>
      </Shell>
    );
  }

  if (phase === "connectedSignInAgain") {
    return (
      <Shell
        icon={CheckCircle2}
        iconClass="text-green-600 dark:text-green-400"
        title={t("connectedTitle", { provider })}
        description={t("connectedMessage", { provider })}
      >
        <Button asChild className="w-full">
          <Link href="/login">{t("signIn")}</Link>
        </Button>
      </Shell>
    );
  }

  const account = (
    <ProviderAccount
      preview={preview}
      label={t("accountLabel", { provider })}
    />
  );

  if (phase === "otherBrowser") {
    return (
      <Shell
        icon={MonitorSmartphone}
        iconClass="text-amber-700 dark:text-amber-300"
        title={t("otherBrowserTitle")}
      >
        {account}
        <p className="text-sm text-foreground">
          {t("otherBrowserMessage", {
            provider,
            login: preview.provider_login,
          })}
        </p>
        <p className="text-sm text-muted-foreground">
          {t("alwaysWorks", { provider })}
        </p>
        {notMeButton}
        {backToLogin}
      </Shell>
    );
  }

  return (
    <Shell icon={Link2} title={t("title", { provider })}>
      {account}
      <p className="text-sm font-medium text-foreground">
        {t("consequence", { provider })}
      </p>
      <Button
        className="w-full"
        disabled={busy !== null}
        onClick={() => void handleConnect()}
      >
        {busy === "connect" ? t("connecting") : t("connect")}
      </Button>
      {notMeButton}
      {backToLogin}
    </Shell>
  );
}

export default function ProviderLinkConfirmPage() {
  return (
    <Suspense fallback={<SpinnerLoading size="lg" />}>
      <ProviderLinkConfirm />
    </Suspense>
  );
}
