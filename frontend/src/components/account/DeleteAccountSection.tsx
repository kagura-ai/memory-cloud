"use client";

/**
 * Self-serve account deletion — danger zone (Issue #953).
 *
 * Mounted at the bottom of the profile page. Drives the SessionUser
 * `/me/account/erasure-*` endpoints (backend done under #360/#469/#486/#489):
 *
 *   1. "Delete my account" → Dialog → Continue → POST erasure-request.
 *   2a. Password-auth users: the response carries `confirm_token`; we show a
 *       password field and POST erasure-confirm (token + password) in-dialog,
 *       which starts the 7-day cooling-off.
 *   2b. OAuth users: `confirm_token` is null and the backend emails a one-time
 *       link to /account/erasure/confirm; we just tell them to check email.
 *   3. While a confirmed request is in cooling-off, the card shows the
 *      scheduled date + a "Cancel deletion" control (DELETE erasure-request).
 *
 * Error surface (see .claude/rules/frontend.md): errors raised inside the
 * Dialog body render as an <Alert variant="destructive">; the cancel action
 * (a button on the card) surfaces via toast. Destructive *text* uses the
 * red-800/red-300 ramp, never `text-destructive` (WCAG AA — recall #957/#958).
 * The confirm token is sensitive and is never logged.
 */

import { useState, useEffect, useCallback } from "react";
import { useTranslations, useLocale } from "next-intl";
import { AlertTriangle } from "lucide-react";

import { useAuth } from "@/contexts/AuthContext";
import { useToast } from "@/hooks/use-toast";
import { formatDate } from "@/lib/utils/datetime";
import { ApiError } from "@/lib/api/base";
import {
  requestErasure,
  confirmErasure,
  cancelErasure,
  getActiveErasureRequest,
  erasureStage,
  type ErasureRequestState,
} from "@/lib/api/account-erasure";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { InlineSpinner } from "@/components/common/LoadingState";

type Step = "intro" | "password" | "emailSent";

export function DeleteAccountSection() {
  const t = useTranslations("accountDeletion");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const { user } = useAuth();
  const { toast } = useToast();

  const [active, setActive] = useState<ErasureRequestState | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<Step>("intro");
  const [token, setToken] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  // Re-sync the active request (used on mount and after a 409 "already in
  // progress" — so the card reflects the existing request instead of looping).
  const loadActive = useCallback(async () => {
    try {
      setActive(await getActiveErasureRequest());
    } catch {
      /* best-effort: a load failure just leaves the delete control available */
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    getActiveErasureRequest()
      .then((r) => mounted && setActive(r))
      .catch(() => {
        /* best-effort: a load failure just leaves the delete button available */
      })
      .finally(() => mounted && setLoaded(true));
    return () => {
      mounted = false;
    };
  }, []);

  const reset = () => {
    setStep("intro");
    setToken(null);
    setPassword("");
    setDialogError(null);
    setBusy(false);
  };

  const onOpenChange = (o: boolean) => {
    setOpen(o);
    if (!o) reset();
  };

  const handleRequest = async () => {
    setBusy(true);
    setDialogError(null);
    try {
      const res = await requestErasure();
      if (res.confirm_token) {
        setToken(res.confirm_token);
        setStep("password");
      } else {
        setStep("emailSent");
      }
    } catch (e) {
      // Branch on the backend error_code, not the HTTP status: ERASURE-003/004
      // both surface as 403 and ERASURE-005/006 both as 409, so status alone
      // can't tell them apart.
      const code = e instanceof ApiError ? e.error : undefined;
      if (code === "ERASURE-004") {
        // Protected initial admin. Defensive fallback — the button is normally
        // hidden for that account, but a stale auth flag shouldn't surface a
        // misleading "try again" for a hard block.
        setDialogError(t("protectedAccount"));
      } else if (code === "ERASURE-005") {
        // Sole owner of a shared workspace — a structural block, not transient.
        setDialogError(t("workspaceTransferError"));
      } else if (code === "ERASURE-006") {
        // An active request already exists (other tab, or a stale pending row).
        // Close and re-sync so the card shows that request instead of looping
        // on a "try again" that can never succeed.
        setOpen(false);
        reset();
        toast({ title: t("alreadyRequested") });
        await loadActive();
      } else {
        setDialogError(t("requestError"));
      }
    } finally {
      setBusy(false);
    }
  };

  const handleConfirmPassword = async () => {
    if (!token) return;
    setBusy(true);
    setDialogError(null);
    try {
      const next = await confirmErasure(token, password);
      setActive(next);
      setOpen(false);
      reset();
    } catch {
      setDialogError(t("confirmError"));
    } finally {
      setBusy(false);
    }
  };

  const handleCancel = async () => {
    setCancelling(true);
    try {
      await cancelErasure();
      setActive(null);
      toast({ title: t("cancelSuccess") });
    } catch {
      toast({ variant: "destructive", title: t("cancelError") });
    } finally {
      setCancelling(false);
    }
  };

  // Avoid flashing the delete button before we know whether a deletion is
  // already scheduled (would briefly contradict the cooling-off state).
  if (!loaded) return null;

  const isInitialAdmin = user?.is_initial_admin === true;
  const stage = erasureStage(active);
  const scheduledLabel =
    active?.scheduled_for &&
    formatDate(active.scheduled_for, user?.timezone ?? "UTC", locale);

  return (
    <Card className="border-red-300 dark:border-red-800/60">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-red-800 dark:text-red-300">
          <AlertTriangle className="h-5 w-5" />
          {t("title")}
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        {isInitialAdmin ? (
          <Alert>
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>{t("protectedAccount")}</AlertDescription>
          </Alert>
        ) : stage === "in_progress" ? (
          // Deletion is executing — no longer cancellable (backend rejects it).
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle className="text-red-800 dark:text-red-300">
              {t("inProgressTitle")}
            </AlertTitle>
            <AlertDescription className="text-red-800 dark:text-red-300">
              {t("inProgressBody")}
            </AlertDescription>
          </Alert>
        ) : stage === "cooling_off" || stage === "pending" ? (
          // Both pending (awaiting confirmation) and cooling_off are cancellable.
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle className="text-red-800 dark:text-red-300">
              {stage === "cooling_off" ? t("scheduledTitle") : t("pendingTitle")}
            </AlertTitle>
            <AlertDescription className="text-red-800 dark:text-red-300">
              {stage === "cooling_off"
                ? t("scheduledBody", { date: scheduledLabel ?? "" })
                : t("pendingBody")}
            </AlertDescription>
            <div className="mt-3">
              <Button
                variant="outline"
                onClick={handleCancel}
                disabled={cancelling}
              >
                {cancelling && <InlineSpinner />}
                {t("cancelButton")}
              </Button>
            </div>
          </Alert>
        ) : (
          <Button variant="destructive" onClick={() => setOpen(true)}>
            {t("deleteButton")}
          </Button>
        )}
      </CardContent>

      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {step === "emailSent" ? t("emailSentTitle") : t("dialogTitle")}
            </DialogTitle>
            <DialogDescription>
              {step === "emailSent" ? t("emailSentBody") : t("dialogIntro")}
            </DialogDescription>
          </DialogHeader>

          {step === "password" && (
            <div className="space-y-2">
              <Label htmlFor="erasure-password">{t("passwordLabel")}</Label>
              <Input
                id="erasure-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={t("passwordPlaceholder")}
              />
              <p className="text-sm text-muted-foreground">
                {t("passwordHint")}
              </p>
            </div>
          )}

          {dialogError && (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertDescription className="text-red-800 dark:text-red-300">
                {dialogError}
              </AlertDescription>
            </Alert>
          )}

          <DialogFooter>
            {step === "emailSent" ? (
              <DialogClose asChild>
                <Button variant="outline">{tCommon("done")}</Button>
              </DialogClose>
            ) : (
              <>
                <DialogClose asChild>
                  <Button variant="outline" disabled={busy}>
                    {t("dialogCancel")}
                  </Button>
                </DialogClose>
                <Button
                  variant="destructive"
                  onClick={step === "password" ? handleConfirmPassword : handleRequest}
                  disabled={busy || (step === "password" && !password.trim())}
                >
                  {busy && <InlineSpinner />}
                  {t("dialogConfirm")}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

export default DeleteAccountSection;
