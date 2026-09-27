"use client";

/**
 * New password + confirmation fields (Issue #1678).
 *
 * Shared by the reset-password and set-password link pages. The mismatch and
 * the server's policy refusal are field-adjacent messages; everything else is
 * the page's to show.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineSpinner } from "@/components/common/LoadingState";

interface NewPasswordFormProps {
  submitLabel: string;
  submittingLabel: string;
  submitting: boolean;
  /** The server refused the password (422): shown under the field. */
  policyError: boolean;
  onSubmit: (newPassword: string) => void;
}

export function NewPasswordForm({
  submitLabel,
  submittingLabel,
  submitting,
  policyError,
  onSubmit,
}: NewPasswordFormProps) {
  const t = useTranslations("passwordRecovery");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [mismatch, setMismatch] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    onSubmit(newPassword);
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="new-password">{t("newPassword")}</Label>
        <Input
          id="new-password"
          type="password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          autoComplete="new-password"
          autoFocus
          aria-describedby="new-password-help"
        />
        <p id="new-password-help" className="text-xs text-muted-foreground">
          {t("requirements")}
        </p>
        {policyError && (
          <p className="text-sm text-red-700 dark:text-red-300" role="alert">
            {t("policyError")}
          </p>
        )}
      </div>
      <div className="space-y-2">
        <Label htmlFor="confirm-password">{t("confirmPassword")}</Label>
        <Input
          id="confirm-password"
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          autoComplete="new-password"
        />
        {mismatch && (
          <p className="text-sm text-red-700 dark:text-red-300" role="alert">
            {t("mismatch")}
          </p>
        )}
      </div>
      <Button
        type="submit"
        className="w-full"
        disabled={submitting || !newPassword || !confirmPassword}
      >
        {submitting && <InlineSpinner className="mr-2" />}
        {submitting ? submittingLabel : submitLabel}
      </Button>
    </form>
  );
}
