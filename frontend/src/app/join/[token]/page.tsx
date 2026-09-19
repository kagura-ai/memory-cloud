"use client";

/**
 * Beta Invite Landing Page (#1582; backend #1581)
 *
 * Public — lives outside `(authenticated)`. A `/join/{token}` link lets one
 * new person through the closed signup gate: this page previews the token and
 * hands it to the OAuth login, where the backend redeems it.
 *
 * Flow (structure mirrors `app/invite/[token]/page.tsx`):
 * 1. Existing session → `already_signed_in`. Invites are for new accounts, so
 *    the token is not even previewed — it stays usable for someone else.
 * 2. Public preview → `valid` / `expired` (410) / not found (404, and any
 *    other failure).
 * 3. Not found is `disabled` when `features.beta_invites` is not on (every
 *    route 404s then, and an older backend has no such route), else `invalid`.
 *    It stays `loading` until the flags are known so it never flips.
 *
 * The token is a credential: never log it, never persist it.
 *
 * Next.js 15: params is a Promise and must be unwrapped with React.use()
 */

import { use, useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import {
  AlertCircle,
  Check,
  Clock,
  Github,
  LogIn,
  MailPlus,
  type LucideIcon,
} from "lucide-react";

import { apiClient, ApiError } from "@/lib/api/base";
import { previewBetaInvite } from "@/lib/api/beta-invites";
import { getAuthConfig } from "@/lib/auth/auth";
import {
  buildOAuthRedirect,
  type OAuthProvider,
} from "@/lib/auth/buildOAuthRedirect";
import { formatDateTime } from "@/lib/utils/datetime";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { Button } from "@/components/ui/button";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { LanguageSelector } from "@/components/LanguageSelector";

type PageState =
  | "loading"
  | "valid"
  | "invalid"
  | "expired"
  | "already_signed_in"
  | "disabled";

// What the two probes found; `not_found` still needs the feature flag to
// become `invalid` or `disabled`.
type Probe =
  | { kind: "pending" }
  | { kind: "signed_in" }
  | { kind: "valid"; expiresAt: string; providers: OAuthProvider[] }
  | { kind: "expired" }
  | { kind: "not_found" };

type Tone = "brand" | "warning" | "danger" | "success";

const TONES: Record<Tone, { circle: string; icon: string }> = {
  brand: {
    circle: "bg-blue-100 dark:bg-blue-900/20",
    icon: "text-blue-600 dark:text-blue-400",
  },
  warning: {
    circle: "bg-amber-100 dark:bg-amber-900/20",
    icon: "text-amber-600 dark:text-amber-400",
  },
  danger: {
    circle: "bg-red-100 dark:bg-red-900/20",
    icon: "text-red-600 dark:text-red-400",
  },
  success: {
    circle: "bg-green-100 dark:bg-green-900/20",
    icon: "text-green-600 dark:text-green-400",
  },
};

function JoinShell({ children }: { children: ReactNode }) {
  return (
    <main className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-4">
      <div className="absolute top-4 right-4">
        <LanguageSelector />
      </div>
      {children}
    </main>
  );
}

function JoinCard({
  icon: Icon,
  tone,
  title,
  children,
}: {
  icon: LucideIcon;
  tone: Tone;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="max-w-md w-full">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-lg p-8">
        <div
          className={`flex items-center justify-center w-16 h-16 rounded-full mx-auto mb-4 ${TONES[tone].circle}`}
        >
          <Icon className={`w-8 h-8 ${TONES[tone].icon}`} aria-hidden="true" />
        </div>
        <h1 className="text-2xl font-bold text-center text-gray-900 dark:text-gray-100 mb-2">
          {title}
        </h1>
        {children}
      </div>
    </div>
  );
}

export default function JoinPage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = use(params);
  const t = useTranslations("betaInvites");
  const locale = useLocale();
  const features = useSystemFeatures();
  const [probe, setProbe] = useState<Probe>({ kind: "pending" });

  useEffect(() => {
    let alive = true;
    setProbe({ kind: "pending" });

    (async (): Promise<Probe> => {
      // Same session check as /invite/[token].
      const signedIn = await apiClient
        .get("/api/v1/auth/me")
        .then(() => true)
        .catch(() => false);
      if (signedIn) return { kind: "signed_in" };

      try {
        const preview = await previewBetaInvite(token);
        // Same provider probe as the login page. A failed probe offers no
        // button rather than one that navigates onto a raw 500.
        const config = await getAuthConfig().catch(() => null);
        const providers: OAuthProvider[] = [];
        if (config?.google_oauth_enabled) providers.push("google");
        if (config?.github_oauth_enabled) providers.push("github");
        return { kind: "valid", expiresAt: preview.expires_at, providers };
      } catch (err) {
        // Deliberately not logged: the request URL carries the token.
        return err instanceof ApiError && err.status === 410
          ? { kind: "expired" }
          : { kind: "not_found" };
      }
    })().then((next) => {
      if (alive) setProbe(next);
    });

    return () => {
      alive = false;
    };
  }, [token]);

  const state: PageState =
    probe.kind === "pending"
      ? "loading"
      : probe.kind === "signed_in"
        ? "already_signed_in"
        : probe.kind === "not_found"
          ? features === null
            ? "loading"
            : features.beta_invites === true
              ? "invalid"
              : "disabled"
          : probe.kind;

  const startSignUp = (provider: OAuthProvider) => {
    window.location.href = buildOAuthRedirect(provider, "/", { invite: token });
  };

  const backToLogin = (
    <Button asChild variant="secondary" className="w-full">
      <Link href="/login">{t("join.backToLogin")}</Link>
    </Button>
  );

  if (state === "loading") {
    return (
      <JoinShell>
        <div className="text-center">
          <SpinnerLoading message={t("join.loading")} />
        </div>
      </JoinShell>
    );
  }

  if (state === "valid" && probe.kind === "valid") {
    return (
      <JoinShell>
        <JoinCard icon={MailPlus} tone="brand" title={t("join.valid.title")}>
          <p className="text-center text-gray-600 dark:text-gray-400 mb-2">
            {t("join.valid.message")}
          </p>
          <p className="text-center text-sm text-gray-600 dark:text-gray-400 mb-6">
            {t("join.valid.expires", {
              date: formatDateTime(
                probe.expiresAt,
                Intl.DateTimeFormat().resolvedOptions().timeZone,
                locale,
              ),
            })}
          </p>

          {probe.providers.includes("google") && (
            <Button
              onClick={() => startSignUp("google")}
              size="lg"
              className="w-full mb-3 text-base [&_svg]:size-5"
            >
              <LogIn />
              {t("join.valid.continueWithGoogle")}
            </Button>
          )}
          {probe.providers.includes("github") && (
            <Button
              onClick={() => startSignUp("github")}
              variant="outline"
              size="lg"
              className="w-full mb-3 text-base [&_svg]:size-5"
            >
              <Github />
              {t("join.valid.continueWithGitHub")}
            </Button>
          )}
          {probe.providers.length === 0 && (
            <p className="text-center text-sm text-gray-600 dark:text-gray-400">
              {t("join.valid.noProviders")}
            </p>
          )}
        </JoinCard>
      </JoinShell>
    );
  }

  if (state === "already_signed_in") {
    return (
      <JoinShell>
        <JoinCard
          icon={Check}
          tone="success"
          title={t("join.alreadySignedIn.title")}
        >
          <p className="text-center text-gray-600 dark:text-gray-400 mb-6">
            {t("join.alreadySignedIn.message")}
          </p>
          <Button asChild className="w-full">
            <Link href="/workspace/dashboard">
              {t("join.alreadySignedIn.goToDashboard")}
            </Link>
          </Button>
        </JoinCard>
      </JoinShell>
    );
  }

  if (state === "expired") {
    return (
      <JoinShell>
        <JoinCard icon={Clock} tone="warning" title={t("join.expired.title")}>
          <p className="text-center text-gray-600 dark:text-gray-400 mb-6">
            {t("join.expired.message")}
          </p>
          {backToLogin}
        </JoinCard>
      </JoinShell>
    );
  }

  if (state === "disabled") {
    return (
      <JoinShell>
        <JoinCard
          icon={AlertCircle}
          tone="warning"
          title={t("join.disabled.title")}
        >
          <p className="text-center text-gray-600 dark:text-gray-400 mb-6">
            {t("join.disabled.message")}
          </p>
          {backToLogin}
        </JoinCard>
      </JoinShell>
    );
  }

  return (
    <JoinShell>
      <JoinCard
        icon={AlertCircle}
        tone="danger"
        title={t("join.invalid.title")}
      >
        <p className="text-center text-gray-600 dark:text-gray-400 mb-6">
          {t("join.invalid.message")}
        </p>
        {backToLogin}
      </JoinCard>
    </JoinShell>
  );
}
