"use client";

/**
 * Beta invite dialog (#1582) — quota, the caller's invites, create and revoke.
 *
 * Pattern: `components/api-keys/CreateAPIKeyDialog.tsx` (one-time secret +
 * Copy). The invite URL is a credential: it exists only in this component's
 * state, from the create response until the dialog closes (or Done). It is
 * never logged and never persisted; the list below never carries a URL.
 *
 * The summary and both mutations come from the sidebar's single
 * `useBetaInvites` instance, so a create / revoke here also updates the card
 * and the account-menu counter.
 */

import { useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { AlertCircle, Check, Copy, Link2, MailPlus } from "lucide-react";

import { useAuth } from "@/contexts/AuthContext";
import { ApiError } from "@/lib/api/base";
import type {
  BetaInvite,
  BetaInviteCreated,
  BetaInviteStatus,
  BetaInviteSummary,
} from "@/lib/api/beta-invites";
import { copyText } from "@/lib/utils/clipboard";
import { formatDateTime } from "@/lib/utils/datetime";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { InlineSpinner, LoadingState } from "@/components/common/LoadingState";
import { cn, colors, typography } from "@/styles/design-tokens";

interface BetaInviteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `null` while the first load is pending (or failed — see `error`). */
  summary: BetaInviteSummary | null;
  error: unknown;
  create: () => Promise<BetaInviteCreated>;
  revoke: (id: string) => Promise<void>;
}

// The label carries the status; the tint only reinforces it.
const STATUS_TINT: Record<BetaInviteStatus, string> = {
  active: colors.badge.success,
  redeemed: colors.badge.info,
  expired: colors.badge.default,
  revoked: colors.badge.default,
};

const CAP_REASON_ID = "beta-invite-cap-reason";
const URL_FIELD_ID = "beta-invite-url";

export function BetaInviteDialog({
  open,
  onOpenChange,
  summary,
  error,
  create,
  revoke,
}: BetaInviteDialogProps) {
  const t = useTranslations("betaInvites");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const { user } = useAuth();

  // One-time display state — cleared on close, see the effect below.
  const [created, setCreated] = useState<BetaInviteCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<BetaInvite | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  // What `open` is right now, for a create that resolves after a close.
  const openRef = useRef(open);

  useEffect(() => {
    openRef.current = open;
    if (open) return;
    setCreated(null);
    setCopied(false);
    setCopyFailed(false);
    setCreateError(null);
    setRevokeTarget(null);
    setRevokeError(null);
  }, [open]);

  const atCap =
    summary !== null && summary.remaining !== null && summary.remaining <= 0;

  const formatWhen = (iso: string) =>
    formatDateTime(iso, user?.timezone, locale);

  const handleCreate = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const next = await create();
      // Closed meanwhile (the owner can drop `open` whatever the guard on the
      // Dialog below says): the clear-on-close effect has already run, so a
      // URL stored now would survive into the next open. Drop it instead.
      if (openRef.current) setCreated(next);
    } catch (err) {
      // 409 quota_exceeded: the hook has already re-read the summary, so the
      // counter and the disabled button now agree with this message.
      setCreateError(
        err instanceof ApiError && err.status === 409
          ? t("dialog.quotaExceeded")
          : t("dialog.createFailed"),
      );
    } finally {
      setCreating(false);
    }
  };

  const handleCopy = async () => {
    if (!created) return;
    try {
      // copyText degrades to an execCommand fallback before throwing (#987).
      // On hard failure the URL stays in the field for a manual copy.
      await copyText(created.url);
      setCopied(true);
      setCopyFailed(false);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopyFailed(true);
    }
  };

  const handleDone = () => {
    setCreated(null);
    setCopied(false);
    setCopyFailed(false);
  };

  const handleRevokeConfirm = async () => {
    if (!revokeTarget) return;
    setRevoking(true);
    setRevokeError(null);
    try {
      await revoke(revokeTarget.id);
      setRevokeTarget(null);
    } catch (err) {
      setRevokeError(
        err instanceof ApiError && err.status === 409
          ? t("dialog.revoke.alreadyRedeemed")
          : t("dialog.revoke.failed"),
      );
    } finally {
      setRevoking(false);
    }
  };

  const whenLine = (invite: BetaInvite): string => {
    if (invite.status === "redeemed" && invite.redeemed_at) {
      return t("dialog.list.redeemedAt", {
        date: formatWhen(invite.redeemed_at),
      });
    }
    if (invite.status === "revoked" && invite.revoked_at) {
      return t("dialog.list.revokedAt", {
        date: formatWhen(invite.revoked_at),
      });
    }
    return t(
      invite.status === "expired"
        ? "dialog.list.expiredAt"
        : "dialog.list.expiresAt",
      { date: formatWhen(invite.expires_at) },
    );
  };

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          // Block close while a create is in flight: the URL is shown once,
          // and a response landing in a closed dialog would be lost to the
          // user (the invite is minted and counted either way).
          if (!next && creating) return;
          onOpenChange(next);
        }}
      >
        <DialogContent className="sm:max-w-[560px]">
          <DialogHeader>
            <DialogTitle>{t("dialog.title")}</DialogTitle>
            <DialogDescription>{t("dialog.description")}</DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            {summary !== null && (
              <p className={cn(typography.label)}>
                {summary.quota === null
                  ? t("dialog.usageUnlimited", { used: summary.used })
                  : t("dialog.usage", {
                      used: summary.used,
                      quota: summary.quota,
                    })}
              </p>
            )}

            {created ? (
              <div className="space-y-3">
                <Alert>
                  <Link2 className="h-4 w-4" />
                  <AlertDescription>
                    {t("dialog.created.onceNote")}
                  </AlertDescription>
                </Alert>
                <div className="space-y-2">
                  <Label htmlFor={URL_FIELD_ID}>
                    {t("dialog.created.urlLabel")}
                  </Label>
                  <div className="flex gap-2">
                    <Input
                      id={URL_FIELD_ID}
                      value={created.url}
                      readOnly
                      className="font-mono text-sm"
                      onFocus={(e) => e.currentTarget.select()}
                    />
                    <Button onClick={handleCopy} variant="outline">
                      {copied ? (
                        <>
                          <Check className="mr-1 h-4 w-4" />
                          {t("dialog.created.copied")}
                        </>
                      ) : (
                        <>
                          <Copy className="mr-1 h-4 w-4" />
                          {t("dialog.created.copy")}
                        </>
                      )}
                    </Button>
                  </div>
                  <p className={typography.caption}>
                    {t("dialog.list.expiresAt", {
                      date: formatWhen(created.expires_at),
                    })}
                  </p>
                  {copyFailed && (
                    <Alert variant="destructive">
                      <AlertCircle className="h-4 w-4" />
                      <AlertDescription>
                        {t("dialog.created.copyFailed")}
                      </AlertDescription>
                    </Alert>
                  )}
                </div>
              </div>
            ) : (
              <div className="space-y-2">
                {createError && (
                  <Alert variant="destructive">
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>{createError}</AlertDescription>
                  </Alert>
                )}
                <Button
                  onClick={handleCreate}
                  disabled={summary === null || atCap || creating}
                  aria-describedby={atCap ? CAP_REASON_ID : undefined}
                >
                  {creating ? (
                    <InlineSpinner className="mr-2" />
                  ) : (
                    <MailPlus className="mr-2 h-4 w-4" />
                  )}
                  {t("dialog.create")}
                </Button>
                {atCap && summary !== null && (
                  <p id={CAP_REASON_ID} className={typography.caption}>
                    {t("dialog.capReached", { quota: summary.quota ?? 0 })}
                  </p>
                )}
              </div>
            )}

            {summary === null ? (
              error ? (
                <Alert variant="destructive">
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{t("dialog.loadFailed")}</AlertDescription>
                </Alert>
              ) : (
                <div data-testid="beta-invite-loading">
                  <LoadingState lines={3} />
                </div>
              )
            ) : summary.invites.length === 0 ? (
              <EmptyState
                compact
                icon={MailPlus}
                title={t("dialog.list.empty")}
                description={t("dialog.list.emptyHint")}
              />
            ) : (
              <div className="space-y-2">
                <h3 className={typography.label}>{t("dialog.list.heading")}</h3>
                <ul
                  className={cn(
                    "max-h-64 divide-y overflow-y-auto rounded-md border",
                    colors.border.default,
                  )}
                >
                  {summary.invites.map((invite) => (
                    <li
                      key={invite.id}
                      className="flex items-center justify-between gap-3 px-3 py-2"
                    >
                      <div className="min-w-0 space-y-1">
                        <Badge
                          variant="outline"
                          className={cn(
                            "border-transparent",
                            STATUS_TINT[invite.status],
                          )}
                        >
                          {t(`dialog.status.${invite.status}`)}
                        </Badge>
                        <p className={typography.caption}>{whenLine(invite)}</p>
                      </div>
                      {invite.status === "active" && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setRevokeError(null);
                            setRevokeTarget(invite);
                          }}
                        >
                          {t("dialog.list.revoke")}
                        </Button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {created && (
            <DialogFooter>
              <Button onClick={handleDone}>{t("dialog.created.done")}</Button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={revokeTarget !== null}
        onOpenChange={(next) => {
          // Block close while a revoke is in flight so the in-dialog error can
          // surface against the open confirm.
          if (!next && !revoking) {
            setRevokeTarget(null);
            setRevokeError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("dialog.revoke.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("dialog.revoke.description")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {revokeError && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>{revokeError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={revoking}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            {/* Regular Button (not AlertDialogAction) so the confirm stays open
                while submitting and on failure; it closes only on success. */}
            <Button
              variant="destructive"
              onClick={handleRevokeConfirm}
              disabled={revoking}
            >
              {revoking && <InlineSpinner className="mr-2" />}
              {t("dialog.revoke.confirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export default BetaInviteDialog;
