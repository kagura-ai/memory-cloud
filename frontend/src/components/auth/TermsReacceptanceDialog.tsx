"use client";

/**
 * The blocking "accept the updated terms" step (#1665).
 *
 * The authenticated layout renders this instead of the app when `/auth/me`
 * reports `terms_acceptance_required` — the deployment's `TERMS_VERSION` is
 * set and differs from the version this user last accepted. There is no way
 * past it but accepting (POST /me/terms-acceptance, then refresh the auth
 * state) or signing out.
 *
 * Built on the Radix dialog primitives (like `components/ui/dialog`), which
 * give `role="dialog"`, `aria-modal`, the title/description wiring and the
 * focus trap. Unlike that wrapper it has no close button, and Escape and
 * outside clicks are swallowed: dismissing it would leave the user in an app
 * they have not agreed to.
 */

import { useState } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { useTranslations } from "next-intl";

import { ApiError } from "@/lib/api/base";
import { acceptTerms } from "@/lib/auth/auth";
import { Button } from "@/components/ui/button";
import { TermsAgreement } from "@/components/auth/TermsAgreement";

type Failure = "stale" | "generic";

export function TermsReacceptanceDialog({
  termsVersion,
  onAccepted,
  onSignOut,
}: {
  /** From `/system/info`; undefined while it loads (the button waits). */
  termsVersion: string | undefined;
  /** Refresh the auth state — the dialog goes away when the flag clears. */
  onAccepted: () => Promise<void>;
  onSignOut: () => void;
}) {
  const t = useTranslations("termsReaccept");
  const [agreed, setAgreed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);

  const accept = async () => {
    if (!termsVersion || !agreed || submitting) return;
    setSubmitting(true);
    setFailure(null);
    try {
      await acceptTerms(termsVersion);
      await onAccepted();
    } catch (err) {
      // 409: the version changed after this page loaded. 404: the deployment
      // turned acceptance off meanwhile — a reload clears the dialog either way.
      const status = err instanceof ApiError ? err.status : null;
      setFailure(status === 409 || status === 404 ? "stale" : "generic");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <DialogPrimitive.Root open>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/80" />
        <DialogPrimitive.Content
          data-testid="terms-reaccept-dialog"
          onEscapeKeyDown={(e) => e.preventDefault()}
          onPointerDownOutside={(e) => e.preventDefault()}
          onInteractOutside={(e) => e.preventDefault()}
          className="fixed left-[50%] top-[50%] z-50 grid w-[calc(100%-2rem)] max-w-lg translate-x-[-50%] translate-y-[-50%] gap-4 rounded-lg border bg-background p-6 shadow-lg"
        >
          <DialogPrimitive.Title className="text-lg font-semibold leading-none tracking-tight">
            {t("title")}
          </DialogPrimitive.Title>
          <DialogPrimitive.Description className="text-sm text-muted-foreground">
            {t("description")}
          </DialogPrimitive.Description>

          <TermsAgreement checked={agreed} onCheckedChange={setAgreed} themed />

          {failure && (
            <p role="alert" className="text-sm text-red-700 dark:text-red-400">
              {failure === "stale" ? t("stale") : t("failed")}
            </p>
          )}

          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button variant="outline" onClick={onSignOut} disabled={submitting}>
              {t("signOut")}
            </Button>
            {failure === "stale" ? (
              <Button onClick={() => window.location.reload()}>
                {t("reload")}
              </Button>
            ) : (
              <Button
                onClick={accept}
                disabled={!termsVersion || !agreed || submitting}
              >
                {submitting ? t("accepting") : t("accept")}
              </Button>
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
