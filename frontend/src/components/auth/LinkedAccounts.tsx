"use client";

/**
 * Linked accounts section (Issue #1784 — one person, two accounts).
 *
 * Lets a signed-in user link another of their own accounts (e.g. a CLI admin
 * and an OAuth account) as one identity: linked accounts own the same private
 * contexts and the memories in them. Roles and workspace membership stay per
 * account. A link is proved by this browser session holding both accounts, so
 * only accounts signed in here (the sidebar account switcher) are linkable.
 *
 * Backend contract:
 *   GET  /api/v1/me/account/identity-links
 *        → { linked: [{user_id, email, name, linked_at}],
 *            linkable: [{user_id, email, name}] }
 *   POST /api/v1/me/account/identity-links         {user_id}
 *        → { status: "ok" } | 404 (not signed in here) | 409 (link set full)
 *   POST /api/v1/me/account/identity-links/unlink  {user_id}
 *        → { status: "ok" } | 404 (not linked)
 */

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
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
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { LoadingState } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";
import { useAuth } from "@/contexts/AuthContext";
import { apiClient, ApiError } from "@/lib/api/base";
import { Loader2, Users } from "lucide-react";

const LINKS_PATH = "/api/v1/me/account/identity-links";

interface IdentityAccount {
  user_id: string;
  email?: string | null;
  name?: string | null;
  linked_at?: string | null;
}

interface IdentityLinksResponse {
  linked: IdentityAccount[];
  linkable: IdentityAccount[];
}

/** What a row calls the account: its name, else its email, else its id. */
function accountLabel(account: IdentityAccount): string {
  return account.name || account.email || account.user_id;
}

export default function LinkedAccounts() {
  const t = useTranslations("linkedAccounts");
  const tCommon = useTranslations("common");
  const { refetchUser } = useAuth();
  const { toast } = useToast();

  const [linked, setLinked] = useState<IdentityAccount[]>([]);
  const [linkable, setLinkable] = useState<IdentityAccount[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // The account whose action (link or unlink) is in flight.
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  // Both actions ask first: linking grants ownership, unlinking removes it.
  const [linkTarget, setLinkTarget] = useState<IdentityAccount | null>(null);
  const [unlinkTarget, setUnlinkTarget] = useState<IdentityAccount | null>(
    null,
  );
  const [dialogError, setDialogError] = useState<string | null>(null);

  const loadLinks = useCallback(async () => {
    setLoadError(null);
    try {
      const data = await apiClient.get<IdentityLinksResponse>(LINKS_PATH);
      setLinked(data.linked ?? []);
      setLinkable(data.linkable ?? []);
    } catch {
      setLoadError(t("loadError"));
    } finally {
      setIsLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadLinks();
  }, [loadLinks]);

  // The list and the auth user (`linked_user_ids`, which owner attribution
  // reads) both change with a link.
  const refreshAfterChange = async () => {
    await Promise.all([loadLinks(), refetchUser()]);
  };

  const handleLinkConfirm = async () => {
    if (!linkTarget) return;
    const account = linkTarget;
    const label = accountLabel(account);
    setBusyUserId(account.user_id);
    setDialogError(null);
    try {
      await apiClient.post(LINKS_PATH, { user_id: account.user_id });
      toast({ title: t("linkSuccess", { account: label }) });
      setLinkTarget(null);
      await refreshAfterChange();
    } catch (error) {
      // Errors raised with the dialog open surface inside it (error-surface
      // rules), not as a toast behind the modal.
      const status = error instanceof ApiError ? error.status : null;
      setDialogError(
        status === 404
          ? t("linkNotSignedInError", { account: label })
          : status === 409
            ? t("linkLimitError")
            : t("linkError", { account: label }),
      );
      // The account left this session: drop the stale row behind the dialog.
      if (status === 404) await loadLinks();
    } finally {
      setBusyUserId(null);
    }
  };

  const handleUnlinkConfirm = async () => {
    if (!unlinkTarget) return;
    const account = unlinkTarget;
    const label = accountLabel(account);
    setBusyUserId(account.user_id);
    setDialogError(null);
    try {
      await apiClient.post(`${LINKS_PATH}/unlink`, {
        user_id: account.user_id,
      });
      toast({ title: t("unlinkSuccess", { account: label }) });
      setUnlinkTarget(null);
      await refreshAfterChange();
    } catch (error) {
      const isNotLinked = error instanceof ApiError && error.status === 404;
      setDialogError(
        isNotLinked
          ? t("unlinkNotLinkedError", { account: label })
          : t("unlinkError", { account: label }),
      );
    } finally {
      setBusyUserId(null);
    }
  };

  const renderRow = (account: IdentityAccount, isLinked: boolean) => {
    const label = accountLabel(account);
    // Under a name, show the email; a row labelled by email or id has
    // nothing further to add.
    const secondary = account.name ? account.email : null;
    return (
      <li
        key={account.user_id}
        className="flex items-center justify-between gap-3 rounded-md border border-slate-200 dark:border-slate-800 p-3"
      >
        <div className="min-w-0">
          <p className="text-sm font-medium leading-none truncate">{label}</p>
          {secondary && (
            <p className="text-xs text-slate-500 mt-1 truncate">{secondary}</p>
          )}
        </div>
        {isLinked ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setDialogError(null);
              setUnlinkTarget(account);
            }}
            disabled={busyUserId !== null}
            aria-label={t("unlinkButtonLabel", { account: label })}
          >
            {t("unlinkButton")}
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setDialogError(null);
              setLinkTarget(account);
            }}
            disabled={busyUserId !== null}
            aria-label={t("linkButtonLabel", { account: label })}
          >
            {t("linkButton")}
          </Button>
        )}
      </li>
    );
  };

  const isEmpty = linked.length === 0 && linkable.length === 0;
  const linkLabel = linkTarget ? accountLabel(linkTarget) : "";
  const unlinkLabel = unlinkTarget ? accountLabel(unlinkTarget) : "";

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
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
              {linked.length > 0 && (
                <section aria-labelledby="linked-accounts-linked-heading">
                  <h3
                    id="linked-accounts-linked-heading"
                    className="text-sm font-medium mb-2"
                  >
                    {t("linkedHeading")}
                  </h3>
                  <ul className="space-y-2">
                    {linked.map((account) => renderRow(account, true))}
                  </ul>
                </section>
              )}
              {linkable.length > 0 && (
                <section aria-labelledby="linked-accounts-linkable-heading">
                  <h3
                    id="linked-accounts-linkable-heading"
                    className="text-sm font-medium mb-2"
                  >
                    {t("linkableHeading")}
                  </h3>
                  <ul className="space-y-2">
                    {linkable.map((account) => renderRow(account, false))}
                  </ul>
                </section>
              )}
              {isEmpty && !loadError && (
                <EmptyState
                  compact
                  icon={Users}
                  title={t("emptyTitle")}
                  description={t("emptyDescription")}
                />
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Link confirmation */}
      <AlertDialog
        open={linkTarget !== null}
        onOpenChange={(open) => {
          // Block close while a link is in flight so the spinner and the
          // in-dialog error Alert can surface against the open dialog.
          if (!open && busyUserId === null) {
            setLinkTarget(null);
            setDialogError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("linkTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {linkTarget ? t("linkDescription", { account: linkLabel }) : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {dialogError && (
            <Alert variant="destructive">
              <AlertDescription>{dialogError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busyUserId !== null}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            {/* Regular Button (not AlertDialogAction) so the dialog stays open
                during submission and the in-dialog error Alert can surface.
                Closes only on success. */}
            <Button onClick={handleLinkConfirm} disabled={busyUserId !== null}>
              {busyUserId !== null && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {busyUserId !== null ? t("linking") : t("linkButton")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Unlink confirmation */}
      <AlertDialog
        open={unlinkTarget !== null}
        onOpenChange={(open) => {
          // Block close while an unlink is in flight so the spinner and the
          // in-dialog error Alert can surface against the open dialog.
          if (!open && busyUserId === null) {
            setUnlinkTarget(null);
            setDialogError(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {unlinkTarget ? t("unlinkTitle", { account: unlinkLabel }) : ""}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {unlinkTarget
                ? t("unlinkDescription", { account: unlinkLabel })
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {dialogError && (
            <Alert variant="destructive">
              <AlertDescription>{dialogError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busyUserId !== null}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            {/* Regular Button (not AlertDialogAction) so the dialog stays open
                during submission and the in-dialog error Alert can surface.
                Closes only on success. */}
            <Button
              variant="destructive"
              onClick={handleUnlinkConfirm}
              disabled={busyUserId !== null}
            >
              {busyUserId !== null && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {busyUserId !== null ? t("unlinking") : t("unlinkButton")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
