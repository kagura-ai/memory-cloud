"use client";

/**
 * "Sign up with email" (#1734): the first step of the email + password
 * sign-up, shown on /join/[token], /invite/[token] and /login.
 *
 * It only asks for the address. The backend answers every request the same
 * way, so the `sent` state never says whether the address has an account or
 * may sign up: it shows the address typed and "if this address can be used,
 * we sent a link". The password is chosen on /signup/verify, after the
 * mailbox owner opened the link.
 *
 * `returnTo` (validated again server-side) stays in this browser
 * (`rememberSignupReturnTo`) and is read back on /signup/verify; it is never
 * part of the request. `invite` is a beta invite token: a credential, never
 * logged or stored.
 *
 * Collapsed to a toggle unless `defaultOpen`. `/login` is always light
 * (#1029); the other pages pass `themed` for the dark-mode colours.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Mail, MailCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineSpinner } from "@/components/common/LoadingState";
import { ApiError } from "@/lib/api/base";
import {
  rememberSignupReturnTo,
  requestEmailSignup,
} from "@/lib/auth/emailSignup";

type Phase = "idle" | "submitting" | "sent";
type Problem = "invalidEmail" | "tooManyRequests" | "genericError";

interface EmailSignupFormProps {
  /** Beta invite token from /join/<token>. */
  invite?: string;
  /** Where to land after the sign-up completes (same-origin). */
  returnTo?: string;
  /** Show the form at once instead of the "Sign up with email" toggle. */
  defaultOpen?: boolean;
  /** Dark-mode colours (pages that follow the theme). */
  themed?: boolean;
}

export function EmailSignupForm({
  invite,
  returnTo,
  defaultOpen = false,
  themed = false,
}: EmailSignupFormProps) {
  const t = useTranslations("emailSignup");
  const [open, setOpen] = useState(defaultOpen);
  const [email, setEmail] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [problem, setProblem] = useState<Problem | null>(null);

  const textClass = themed
    ? "text-sm text-gray-600 dark:text-gray-400"
    : "text-sm text-gray-600";
  const labelClass = themed
    ? "text-gray-700 dark:text-gray-300"
    : "text-gray-700";

  if (!open) {
    return (
      <Button
        type="button"
        variant="outline"
        size="lg"
        className="w-full text-base [&_svg]:size-5"
        onClick={() => setOpen(true)}
      >
        <Mail />
        {t("request.toggle")}
      </Button>
    );
  }

  if (phase === "sent") {
    return (
      <div role="status" className="space-y-3 text-center">
        <MailCheck
          className="mx-auto h-8 w-8 text-green-600 dark:text-green-400"
          aria-hidden="true"
        />
        <p
          className={
            themed
              ? "font-semibold text-gray-900 dark:text-gray-100"
              : "font-semibold text-gray-900"
          }
        >
          {t("request.sentTitle")}
        </p>
        <p className={textClass}>{t("request.sentMessage", { email })}</p>
        <Button
          type="button"
          variant="link"
          className="text-sm"
          onClick={() => {
            setPhase("idle");
            setEmail("");
          }}
        >
          {t("request.useAnother")}
        </Button>
      </div>
    );
  }

  const submitting = phase === "submitting";
  const trimmed = email.trim();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!trimmed || submitting) return;
    setPhase("submitting");
    setProblem(null);
    // Kept before the request: an older sign-up's path must never apply.
    rememberSignupReturnTo(returnTo);
    try {
      await requestEmailSignup({ email: trimmed, invite });
      setPhase("sent");
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      setProblem(
        status === 422
          ? "invalidEmail"
          : status === 429
            ? "tooManyRequests"
            : "genericError",
      );
      setPhase("idle");
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-2" noValidate>
      <Label htmlFor="email-signup-address" className={labelClass}>
        {t("request.emailLabel")}
      </Label>
      <Input
        id="email-signup-address"
        type="email"
        value={email}
        onChange={(e) => {
          setEmail(e.target.value);
          setProblem(null);
        }}
        placeholder={t("request.emailPlaceholder")}
        autoComplete="email"
        autoFocus={!defaultOpen}
        maxLength={254}
        disabled={submitting}
        aria-invalid={problem === "invalidEmail"}
        aria-describedby={problem ? "email-signup-problem" : undefined}
        className={themed ? undefined : "bg-white text-gray-900"}
      />
      {problem && (
        <p
          id="email-signup-problem"
          role="alert"
          className={
            themed
              ? "text-sm text-red-700 dark:text-red-300"
              : "text-sm text-red-700"
          }
        >
          {t(`request.${problem}`)}
        </p>
      )}
      <Button
        type="submit"
        size="lg"
        className="w-full text-base"
        disabled={submitting || !trimmed}
      >
        {submitting && <InlineSpinner className="mr-2" />}
        {submitting ? t("request.submitting") : t("request.submit")}
      </Button>
    </form>
  );
}
