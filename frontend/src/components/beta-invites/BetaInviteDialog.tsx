"use client";

/**
 * Beta invite dialog (#1582, #1595) — quota, the caller's invites, create,
 * revoke and reissue.
 *
 * Pattern: `components/api-keys/CreateAPIKeyDialog.tsx` (one-time secret +
 * Copy). The invite URL is a credential: it exists only in this component's
 * state, from the create / reissue response until the dialog closes (or Done).
 * It is never logged and never persisted; the list below never carries a URL.
 *
 * #1595: a link can carry an inviter-private label, a redeemed row names the
 * account it admitted, and "Reissue" replaces a lost link in one click — the
 * stored hash cannot be shown again, so the old link is revoked and the new URL
 * lands in the same one-time panel. Labels and addresses are rendered, never
 * logged.
 *
 * The summary and the mutations come from the sidebar's single
 * `useBetaInvites` instance, so a create / revoke / reissue here also updates
 * the card and the account-menu counter.
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
  create: (label?: string) => Promise<BetaInviteCreated>;
  revoke: (id: string) => Promise<void>;
  reissue: (id: string) => Promise<BetaInviteCreated>;
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
const LABEL_FIELD_ID = "beta-invite-label";
const LABEL_HELP_ID = "beta-invite-label-help";
// Longest label the API accepts — mirrors `beta_invites.label` VARCHAR(100).
const LABEL_MAX_LENGTH = 100;

export function BetaInviteDialog({
  open,
  onOpenChange,
  summary,
  error,
  create,
  revoke,
  reissue,
}: BetaInviteDialogProps) {
  const t = useTranslations("betaInvites");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const { user } = useAuth();

  // One-time display state — cleared on close, see the effect below.
  const [created, setCreated] = useState<BetaInviteCreated | null>(null);
  // The URL on screen replaces a link that just stopped working.
  const [createdByReissue, setCreatedByReissue] = useState(false);
  const [label, setLabel] = useState("");
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<BetaInvite | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [reissuingId, setReissuingId] = useState<string | null>(null);
  const [reissueError, setReissueError] = useState<string | null>(null);
  // What `open` is right now, for a create / reissue that resolves after a close.
  const openRef = useRef(open);

  useEffect(() => {
    openRef.current = open;
    if (open) return;
    setCreated(null);
    setCreatedByReissue(false);
    setLabel("");
    setCopied(false);
    setCopyFailed(false);
    setCreateError(null);
    setRevokeTarget(null);
    setRevokeError(null);
    setReissueError(null);
  }, [open]);

  const atCap =
    summary !== null && summary.remaining !== null && summary.remaining <= 0;
  // There is ONE URL panel, so mints never overlap: a second response would
  // replace a URL the user has not copied yet. (Reissue additionally waits for
  // Done while a URL is on screen — see the row buttons.)
  const minting = creating || reissuingId !== null;

  const formatWhen = (iso: string) =>
    formatDateTime(iso, user?.timezone, locale);

  const handleCreate = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const next = await create(label);
      // Closed meanwhile (the owner can drop `open` whatever the guard on the
      // Dialog below says): the clear-on-close effect has already run, so a
      // URL stored now would survive into the next open. Drop it instead.
      if (openRef.current) {
        setCreated(next);
        setCreatedByReissue(false);
        setLabel("");
      }
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

  const handleReissue = async (invite: BetaInvite) => {
    setReissuingId(invite.id);
    setReissueError(null);
    try {
      const next = await reissue(invite.id);
      // Same late-arrival rule as `handleCreate`.
      if (openRef.current) {
        setCreated(next);
        setCreatedByReissue(true);
        setCopied(false);
        setCopyFailed(false);
      }
    } catch (err) {
      // Every 409 has already made the hook re-read the list.
      const code =
        err instanceof ApiError && err.status === 409 ? err.error : null;
      if (code === "BETA-INVITE-003") {
        // Already revoked — a double-click whose first request won. The fresh
        // list shows the outcome; there is nothing to tell the user.
      } else if (code === "BETA-INVITE-002") {
        setReissueError(t("dialog.reissue.alreadyRedeemed"));
      } else if (code === "BETA-INVITE-001") {
        setReissueError(t("dialog.quotaExceeded"));
      } else {
        setReissueError(t("dialog.reissue.failed"));
      }
    } finally {
      setReissuingId(null);
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
    setCreatedByReissue(false);
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
          // Block close while a create / reissue is in flight: the URL is
          // shown once, and a response landing in a closed dialog would be
          // lost to the user (the invite is minted and counted either way).
          if (!next && minting) return;
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
                  ? t("dialog.usageUnlimited", {
                      active: summary.active,
                      redeemed: summary.redeemed,
                    })
                  : t("dialog.usage", {
                      active: summary.active,
                      redeemed: summary.redeemed,
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
                    {createdByReissue && (
                      <span className="mt-1 block">
                        {t("dialog.created.reissuedNote")}
                      </span>
                    )}
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
                <div className="space-y-1">
                  <Label htmlFor={LABEL_FIELD_ID}>
                    {t("dialog.labelField.label")}
                  </Label>
                  <Input
                    id={LABEL_FIELD_ID}
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    maxLength={LABEL_MAX_LENGTH}
                    placeholder={t("dialog.labelField.placeholder")}
                    aria-describedby={LABEL_HELP_ID}
                    autoComplete="off"
                    disabled={creating}
                  />
                  <p id={LABEL_HELP_ID} className={typography.caption}>
                    {t("dialog.labelField.help")}
                  </p>
                </div>
                <Button
                  onClick={handleCreate}
                  disabled={summary === null || atCap || minting}
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
                {reissueError && (
                  <Alert variant="destructive">
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>{reissueError}</AlertDescription>
                  </Alert>
                )}
                <ul
                  className={cn(
                    "max-h-64 divide-y overflow-y-auto rounded-md border",
                    colors.border.default,
                  )}
                >
                  {summary.invites.map((invite) => {
                    const reissuable =
                      invite.status === "active" || invite.status === "expired";
                    const rowBusy = reissuingId === invite.id;
                    return (
                      // Wraps instead of squeezing: on a 320 px screen the
                      // buttons drop below the text rather than crushing it.
                      <li
                        key={invite.id}
                        data-testid={`beta-invite-row-${invite.id}`}
                        className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 px-3 py-2"
                      >
                        <div className="min-w-0 flex-1 basis-40 space-y-1">
                          <Badge
                            variant="outline"
                            className={cn(
                              "border-transparent",
                              STATUS_TINT[invite.status],
                            )}
                          >
                            {t(`dialog.status.${invite.status}`)}
                          </Badge>
                          {invite.label && (
                            <p
                              data-testid="beta-invite-label"
                              className={cn(typography.label, "truncate")}
                              title={invite.label}
                            >
                              {invite.label}
                            </p>
                          )}
                          {invite.status === "redeemed" &&
                            invite.redeemed_email && (
                              <p
                                data-testid="beta-invite-redeemed-email"
                                className={cn(typography.caption, "truncate")}
                                title={invite.redeemed_email}
                              >
                                {t("dialog.list.redeemedBy", {
                                  email: invite.redeemed_email,
                                })}
                              </p>
                            )}
                          <p className={typography.caption}>
                            {whenLine(invite)}
                          </p>
                        </div>
                        {reissuable && (
                          <div className="flex shrink-0 gap-2">
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => void handleReissue(invite)}
                              disabled={minting || created !== null}
                            >
                              {rowBusy && <InlineSpinner className="mr-2" />}
                              {t("dialog.list.reissue")}
                            </Button>
                            {invite.status === "active" && (
                              <Button
                                variant="outline"
                                size="sm"
                                disabled={rowBusy}
                                onClick={() => {
                                  setRevokeError(null);
                                  setRevokeTarget(invite);
                                }}
                              >
                                {t("dialog.list.revoke")}
                              </Button>
                            )}
                          </div>
                        )}
                      </li>
                    );
                  })}
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
