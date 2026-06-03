"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Plug, Trash2 } from "lucide-react";

import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useToast } from "@/hooks/use-toast";
import {
  createConnector,
  deleteConnector,
  getSlackPendingInstall,
  listConnectors,
  slackInstallUrl,
  type CreateConnectorResponse,
  type SlackPendingInstall,
  type WorkspaceConnectorSummary,
} from "@/lib/api/workspace-connectors";

// Slugify into the backend's resource_id charset (^[a-z0-9_-]+$). Capped at 100
// so the derived value also satisfies the backend's auto_create_context_name
// limit (100); resource_id allows up to 255 so 100 is safe for both uses.
const CONNECTOR_NAME_MAX = 100;

function toResourceId(seed: string): string {
  const slug = seed
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `slack-${slug || "team"}`.slice(0, CONNECTOR_NAME_MAX);
}

export default function ConnectorsPage() {
  const t = useTranslations("connectors");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const router = useRouter();
  const searchParams = useSearchParams();
  const installHandle = searchParams.get("slack_install");

  const [connectors, setConnectors] = useState<
    WorkspaceConnectorSummary[] | null
  >(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Slack install → create dialog
  const [pending, setPending] = useState<SlackPendingInstall | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [contextName, setContextName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // One-time credentials reveal after a successful create
  const [created, setCreated] = useState<CreateConnectorResponse | null>(null);

  // Delete confirmation
  const [toDelete, setToDelete] = useState<WorkspaceConnectorSummary | null>(
    null,
  );

  const reload = useCallback(async () => {
    try {
      setLoadError(null);
      setConnectors(await listConnectors());
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // After the Slack OAuth callback redirects back with ?slack_install=<handle>,
  // fetch the non-secret install summary and open the create dialog.
  useEffect(() => {
    if (!installHandle) return;
    let cancelled = false;
    (async () => {
      try {
        const info = await getSlackPendingInstall(installHandle);
        if (cancelled) return;
        setPending(info);
        const seed = info.team_name || info.team_id;
        setDisplayName(info.team_name || info.team_id);
        setContextName(toResourceId(seed));
      } catch {
        if (!cancelled) {
          toast({
            variant: "destructive",
            title: t("installExpiredTitle"),
            description: t("installExpiredDesc"),
          });
          // Strip the stale ?slack_install param so a page refresh or
          // navigation back doesn't re-trigger this toast on every visit.
          router.replace("/workspace/integrations/connectors");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [installHandle, t, toast, router]);

  const closeCreateDialog = useCallback(() => {
    setPending(null);
    setCreateError(null);
    // Drop the one-time handle from the URL so a refresh doesn't re-trigger.
    router.replace("/workspace/integrations/connectors");
  }, [router]);

  const handleCreate = useCallback(async () => {
    if (!installHandle || !pending) return;
    setSubmitting(true);
    setCreateError(null);
    try {
      const result = await createConnector({
        connector_type: "slack",
        resource_id: toResourceId(pending.team_id),
        display_name: displayName || undefined,
        auto_create_context_name: contextName || undefined,
        slack_install_handle: installHandle,
        pii_guardrail_config: { enabled: true },
      });
      setPending(null);
      setCreated(result);
      router.replace("/workspace/integrations/connectors");
      await reload();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }, [installHandle, pending, displayName, contextName, router, reload]);

  const handleDelete = useCallback(async () => {
    if (!toDelete) return;
    const target = toDelete;
    setToDelete(null);
    try {
      await deleteConnector(target.connector_id);
      toast({ title: t("connectorDeleted") });
      await reload();
    } catch (err) {
      toast({
        variant: "destructive",
        title: t("deleteFailed"),
        description: err instanceof Error ? err.message : String(err),
      });
    }
  }, [toDelete, t, toast, reload]);

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      <div className="mb-4 flex justify-end">
        <Button onClick={() => (window.location.href = slackInstallUrl())}>
          {t("connectSlack")}
        </Button>
      </div>

      {loadError ? (
        <ErrorBanner error={loadError} />
      ) : connectors === null ? (
        <TableLoadingState rows={3} />
      ) : connectors.length === 0 ? (
        <EmptyState
          icon={Plug}
          title={t("emptyTitle")}
          description={t("emptyDesc")}
          actionLabel={t("connectSlack")}
          onAction={() => (window.location.href = slackInstallUrl())}
        />
      ) : (
        <ul className="divide-y rounded-md border">
          {connectors.map((c) => (
            <li
              key={c.connector_id}
              className="flex items-center justify-between p-4"
            >
              <div>
                <p className="font-medium capitalize">{c.connector_type}</p>
                <p className="text-sm text-muted-foreground">
                  {c.context_id
                    ? t("contextBound", { id: c.context_id })
                    : t("contextNotReady")}
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setToDelete(c)}
                aria-label={tCommon("delete")}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      {/* Create dialog (after Slack OAuth) */}
      <AlertDialog
        open={pending !== null}
        onOpenChange={(o) => !o && closeCreateDialog()}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("createTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("createDesc", {
                team: pending?.team_name || pending?.team_id || "",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-4">
            {createError && (
              <Alert variant="destructive">
                <AlertDescription>{createError}</AlertDescription>
              </Alert>
            )}
            <div>
              <label
                htmlFor="conn-display-name"
                className="mb-1 block text-sm font-medium"
              >
                {t("displayName")}
              </label>
              <Input
                id="conn-display-name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </div>
            <div>
              <label
                htmlFor="conn-context-name"
                className="mb-1 block text-sm font-medium"
              >
                {t("contextName")}
              </label>
              <Input
                id="conn-context-name"
                value={contextName}
                maxLength={CONNECTOR_NAME_MAX}
                onChange={(e) => setContextName(e.target.value)}
              />
              <p className="mt-1 text-xs text-muted-foreground">
                {t("contextNameHelp")}
              </p>
            </div>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel
              onClick={closeCreateDialog}
              disabled={submitting}
            >
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleCreate}
              disabled={submitting || !contextName}
            >
              {t("createConnector")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* One-time credentials reveal */}
      <AlertDialog
        open={created !== null}
        onOpenChange={(o) => !o && setCreated(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("createdTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("createdDesc")}</AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-3 text-sm">
            <div>
              <p className="font-medium">{t("connectorId")}</p>
              <code className="break-all">{created?.connector_id}</code>
            </div>
            {created?.kmc_api_key && (
              <div>
                <p className="font-medium">{t("kmcApiKey")}</p>
                <code className="break-all">{created.kmc_api_key}</code>
              </div>
            )}
            {created?.token && (
              <div>
                <p className="font-medium">{t("resourceToken")}</p>
                <code className="break-all">{created.token}</code>
              </div>
            )}
          </div>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setCreated(null)}>
              {tCommon("done")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete confirmation */}
      <AlertDialog
        open={toDelete !== null}
        onOpenChange={(o) => !o && setToDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("deleteDesc")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete}>
              {tCommon("delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
