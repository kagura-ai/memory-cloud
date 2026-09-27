"use client";

/**
 * Password section of the profile page (Issue #1678).
 *
 * - No password: "Set up a password" emails a one-time link to the account's
 *   address (following it proves the mailbox, then sets the password).
 * - Password set: change it (current + new + confirm), or remove it — refused
 *   while no Google/GitHub account is linked, so the last sign-in method can
 *   never be removed.
 *
 * Backend contract:
 *   POST   /api/v1/me/password/setup-request → 202 | 409 | 429 | 503
 *   POST   /api/v1/me/password/change        → 204 | 403 | 422 | 429
 *   DELETE /api/v1/me/password               → 204 | 403 | 409 | 429
 *   GET    /api/v1/me/account/providers      (how many OAuth methods remain)
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { KeyRound, MailCheck } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { InlineSpinner, LoadingState } from "@/components/common/LoadingState";
import { useAuth } from "@/contexts/AuthContext";
import { useToast } from "@/hooks/use-toast";
import { apiClient, ApiError } from "@/lib/api/base";
import {
  changePassword,
  removePassword,
  requestPasswordSetup,
} from "@/lib/auth/auth";

interface ProvidersResponse {
  providers: { provider: string }[];
}

function statusOf(err: unknown): number {
  return err instanceof ApiError ? err.status : 0;
}

export default function PasswordSettings() {
  const t = useTranslations("passwordSettings");
  const tCommon = useTranslations("common");
  const { user, refetchUser } = useAuth();
  const { toast } = useToast();

  // `has_password` (#1678) is authoritative; an older backend only sends
  // `auth_method`, where "password" implied one.
  const hasPassword = user?.has_password ?? user?.auth_method === "password";

  const [linkedCount, setLinkedCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Set-up request
  const [sendingSetup, setSendingSetup] = useState(false);
  const [setupSent, setSetupSent] = useState(false);

  // Change form
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [changing, setChanging] = useState(false);
  const [changeFieldError, setChangeFieldError] = useState<
    "wrongCurrent" | "mismatch" | "policyError" | null
  >(null);

  // Remove dialog
  const [removeOpen, setRemoveOpen] = useState(false);
  const [removePasswordInput, setRemovePasswordInput] = useState("");
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const loadProviders = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await apiClient.get<ProvidersResponse>(
        "/api/v1/me/account/providers",
      );
      setLinkedCount(data.providers?.length ?? 0);
    } catch {
      setLoadError(t("loadError"));
    } finally {
      setIsLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadProviders();
  }, [loadProviders]);

  const canRemove = linkedCount > 0;

  const handleSetupRequest = async () => {
    setSendingSetup(true);
    try {
      await requestPasswordSetup();
      setSetupSent(true);
    } catch (err) {
      const status = statusOf(err);
      if (status === 409) {
        // Already has a password (set elsewhere) — refresh the view.
        await refetchUser();
      } else {
        toast({
          title: tCommon("error"),
          description: status === 429 ? t("tooManyRequests") : t("setupError"),
          variant: "destructive",
        });
      }
    } finally {
      setSendingSetup(false);
    }
  };

  const handleChange = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      setChangeFieldError("mismatch");
      return;
    }
    setChangeFieldError(null);
    setChanging(true);
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      toast({ title: t("changeSuccess") });
    } catch (err) {
      const status = statusOf(err);
      if (status === 403) setChangeFieldError("wrongCurrent");
      else if (status === 422) setChangeFieldError("policyError");
      else
        toast({
          title: tCommon("error"),
          description: status === 429 ? t("tooManyRequests") : t("changeError"),
          variant: "destructive",
        });
    } finally {
      setChanging(false);
    }
  };

  const handleRemove = async () => {
    setRemoving(true);
    setRemoveError(null);
    try {
      await removePassword(removePasswordInput);
      setRemoveOpen(false);
      setRemovePasswordInput("");
      toast({ title: t("removeSuccess") });
      await refetchUser();
    } catch (err) {
      const status = statusOf(err);
      setRemoveError(
        status === 403
          ? t("wrongCurrent")
          : status === 409
            ? t("lastMethodError")
            : status === 429
              ? t("tooManyRequests")
              : t("removeError"),
      );
    } finally {
      setRemoving(false);
    }
  };

  const fieldError = (text: string) => (
    <p className="text-sm text-red-700 dark:text-red-300" role="alert">
      {text}
    </p>
  );

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-5 w-5" />
            {t("title")}
          </CardTitle>
          <CardDescription>{t("description")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {isLoading ? (
            <LoadingState lines={2} />
          ) : (
            <>
              <ErrorBanner error={loadError} />
              <p className="text-sm text-slate-600 dark:text-slate-400">
                {hasPassword ? t("statusSet") : t("statusNotSet")}
              </p>

              {!hasPassword && (
                <>
                  {setupSent ? (
                    <Alert>
                      <MailCheck className="h-4 w-4" />
                      <AlertDescription>
                        {t("setupSent", { email: user?.email ?? "" })}
                      </AlertDescription>
                    </Alert>
                  ) : (
                    <Button
                      variant="outline"
                      onClick={() => void handleSetupRequest()}
                      disabled={sendingSetup}
                    >
                      {sendingSetup && <InlineSpinner className="mr-2" />}
                      {sendingSetup ? t("setupSending") : t("setupButton")}
                    </Button>
                  )}
                </>
              )}

              {hasPassword && (
                <>
                  <form onSubmit={handleChange} className="space-y-3">
                    <div className="space-y-2">
                      <Label htmlFor="current-password">
                        {t("currentPassword")}
                      </Label>
                      <Input
                        id="current-password"
                        type="password"
                        value={currentPassword}
                        onChange={(e) => setCurrentPassword(e.target.value)}
                        autoComplete="current-password"
                      />
                      {changeFieldError === "wrongCurrent" &&
                        fieldError(t("wrongCurrent"))}
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="new-password">{t("newPassword")}</Label>
                      <Input
                        id="new-password"
                        type="password"
                        value={newPassword}
                        onChange={(e) => setNewPassword(e.target.value)}
                        autoComplete="new-password"
                      />
                      <p className="text-xs text-muted-foreground">
                        {t("requirements")}
                      </p>
                      {changeFieldError === "policyError" &&
                        fieldError(t("policyError"))}
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="confirm-new-password">
                        {t("confirmPassword")}
                      </Label>
                      <Input
                        id="confirm-new-password"
                        type="password"
                        value={confirmPassword}
                        onChange={(e) => setConfirmPassword(e.target.value)}
                        autoComplete="new-password"
                      />
                      {changeFieldError === "mismatch" &&
                        fieldError(t("mismatch"))}
                    </div>
                    <Button
                      type="submit"
                      disabled={
                        changing ||
                        !currentPassword ||
                        !newPassword ||
                        !confirmPassword
                      }
                    >
                      {changing && <InlineSpinner className="mr-2" />}
                      {changing ? t("changing") : t("changeButton")}
                    </Button>
                  </form>

                  <div className="flex flex-col items-start gap-1 border-t border-slate-200 pt-4 dark:border-slate-800">
                    <Button
                      variant="outline"
                      onClick={() => {
                        setRemoveError(null);
                        setRemovePasswordInput("");
                        setRemoveOpen(true);
                      }}
                      disabled={!canRemove}
                    >
                      {t("removeButton")}
                    </Button>
                    {!canRemove && (
                      <p className="text-xs text-slate-500">
                        {t("removeDisabledHint")}
                      </p>
                    )}
                  </div>
                </>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={removeOpen}
        onOpenChange={(open) => {
          if (!open && !removing) {
            setRemoveOpen(false);
            setRemoveError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("removeTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("removeDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2">
            <Label htmlFor="remove-current-password">
              {t("currentPassword")}
            </Label>
            <Input
              id="remove-current-password"
              type="password"
              value={removePasswordInput}
              onChange={(e) => setRemovePasswordInput(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          {removeError && (
            <Alert variant="destructive">
              <AlertDescription>{removeError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removing}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={() => void handleRemove()}
              disabled={removing || !removePasswordInput}
            >
              {removing && <InlineSpinner className="mr-2" />}
              {removing ? t("removing") : t("removeConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
