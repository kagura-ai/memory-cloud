"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { Check, Copy, Plug, Trash2 } from "lucide-react";

import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import {
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { API_BASE_URL } from "@/lib/api/base";
import {
  createConnector,
  deleteConnector,
  getSlackPendingInstall,
  listAvailableWorkerApps,
  listConnectors,
  slackInstallUrl,
  updateConnectorRuntime,
  type AvailableWorkerApp,
  type CreateConnectorResponse,
  type SlackPendingInstall,
  type WorkspaceConnectorSummary,
} from "@/lib/api/workspace-connectors";

// Slugify into the backend's resource_id charset (^[a-z0-9_-]+$). Capped at 100
// so the derived value also satisfies the backend's auto_create_context_name
// limit (100); resource_id allows up to 255 so 100 is safe for both uses.
const CONNECTOR_NAME_MAX = 100;

// #890: Presidio recognizer names offered in the PII config UI. Kept in sync
// with the worker's recognizer set; the backend validates the full
// PiiGuardrailConfig shape (extra keys forbidden, redaction enum, non-blank locale,
// and requires detectors when enabled).
const PII_DETECTORS = [
  "EMAIL_ADDRESS",
  "PHONE_NUMBER",
  "CREDIT_CARD",
  "PERSON",
  "IP_ADDRESS",
  "IBAN_CODE",
] as const;
const PII_DEFAULT_DETECTORS = [
  "EMAIL_ADDRESS",
  "PHONE_NUMBER",
  "CREDIT_CARD",
  "PERSON",
];
const PII_REDACTION_MODES = ["mask", "hash", "remove"] as const;
type PiiRedaction = (typeof PII_REDACTION_MODES)[number];

// #893: copy-pastable curl against the resource-ingest API for manual CLI
// testing (verify events become memories without a worker). Single-quote the
// header value so a token with shell metacharacters is safe to paste.
function curlSample(resourceId: string, token: string): string {
  return [
    `curl -X POST '${API_BASE_URL}/api/v1/resources/${resourceId}/events' \\`,
    `  -H 'X-Resource-API-Key: ${token}' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d '{"op":"upsert","doc_id":"test-1","payload":{"text":"hello"}}'`,
  ].join("\n");
}

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
  const locale = useLocale();
  const { toast } = useToast();
  const { isCopied, copyToTarget } = useCopyFeedback();
  const router = useRouter();
  const searchParams = useSearchParams();

  // Client-side RBAC gate (#903). All connector endpoints require workspace
  // ADMIN/OWNER (backend `require_workspace_admin` is the source of truth);
  // without this gate a member/viewer would hit a 403 on the list load and
  // see a broken page with action buttons that always fail. This is
  // defense-in-depth UX, not a security boundary.
  const { currentWorkspace, currentWorkspaceId, loading } = useWorkspace();
  const allowed = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Admin,
  );

  // Copy with the shared per-key feedback hook (unmount-safe, 2000ms standard);
  // surface clipboard failures via the destructive-toast channel.
  const handleCopy = useCallback(
    async (text: string, key: string) => {
      try {
        await copyToTarget(text, key);
      } catch {
        toast({ variant: "destructive", title: tCommon("error") });
      }
    },
    [copyToTarget, toast, tCommon],
  );
  const installHandle = searchParams.get("slack_install");
  const slackError = searchParams.get("slack_error");

  const [connectors, setConnectors] = useState<
    WorkspaceConnectorSummary[] | null
  >(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // #1360: available-apps panel failure is decoupled from the primary
  // connectors list — its own banner, never a page takedown.
  const [appsLoadError, setAppsLoadError] = useState<string | null>(null);
  const [availableApps, setAvailableApps] = useState<
    AvailableWorkerApp[] | null
  >(null);

  // Manual binding is the multi-app path: a workspace admin selects a global
  // app identity and supplies that installation's bot token + Slack team id.
  // The token is sent once to memory-cloud and stored Fernet-encrypted; worker
  // config remains entirely server-managed.
  const [manualAppKey, setManualAppKey] = useState("");
  const [manualTeamId, setManualTeamId] = useState("");
  const [manualBotToken, setManualBotToken] = useState("");
  const [manualSubmitting, setManualSubmitting] = useState(false);
  const [manualError, setManualError] = useState<string | null>(null);

  // Slack install → create dialog
  const [pending, setPending] = useState<SlackPendingInstall | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [contextName, setContextName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // #890: PII guardrail config for the create form. Defaults scrub on by
  // default so an admin who touches nothing still ships a safe config.
  const [piiEnabled, setPiiEnabled] = useState(true);
  const [piiDetectors, setPiiDetectors] = useState<string[]>(
    PII_DEFAULT_DETECTORS,
  );
  const [piiRedaction, setPiiRedaction] = useState<PiiRedaction>("mask");
  const [piiFailClosed, setPiiFailClosed] = useState(true);

  // One-time credentials reveal after a successful create
  const [created, setCreated] = useState<CreateConnectorResponse | null>(null);

  // Delete confirmation
  const [toDelete, setToDelete] = useState<WorkspaceConnectorSummary | null>(
    null,
  );
  const [runtimeSaving, setRuntimeSaving] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setLoadError(null);
      // #1360: allSettled, not all — the connectors list is the page's
      // primary content and must not be taken down by a failure of the
      // auxiliary available-apps lookup (the app selector just degrades).
      const [connectorResult, appResult] = await Promise.allSettled([
        listConnectors(),
        listAvailableWorkerApps(),
      ]);
      if (connectorResult.status === "rejected") {
        throw connectorResult.reason;
      }
      setConnectors(connectorResult.value);
      if (appResult.status === "fulfilled") {
        setAppsLoadError(null);
        const slackApps = appResult.value.filter(
          (app) => app.platform === "slack",
        );
        setAvailableApps(slackApps);
        setManualAppKey((current) =>
          slackApps.some((app) => app.app_key === current)
            ? current
            : (slackApps[0]?.app_key ?? ""),
        );
      } else {
        // Keep whatever list we already had (a transient refresh failure
        // must not wipe a working selector mid-session) and surface the
        // degradation via a panel-level banner instead of vanishing
        // silently — null vs [] would otherwise be indistinguishable.
        setAppsLoadError(
          appResult.reason instanceof Error
            ? appResult.reason.message
            : String(appResult.reason),
        );
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    // Don't fire the admin-only list call for non-admins — it would 403.
    if (!allowed) return;
    void reload();
    // Key on currentWorkspaceId too: switching workspace (while staying
    // admin/owner) must refetch so stale connectors from the previous
    // workspace aren't left rendered. listConnectors is workspace-scoped.
  }, [reload, allowed, currentWorkspaceId]);

  // After the Slack OAuth callback redirects back with ?slack_install=<handle>,
  // fetch the non-secret install summary and open the create dialog.
  useEffect(() => {
    if (!installHandle) return;
    if (!allowed) return;
    let cancelled = false;
    (async () => {
      try {
        const info = await getSlackPendingInstall(installHandle);
        if (cancelled) return;
        setPending(info);
        const seed = info.team_name || info.team_id;
        setDisplayName(info.team_name || info.team_id);
        setContextName(toResourceId(seed));
        // Reset PII config to safe defaults for each new install session so a
        // prior session's choices don't leak into a fresh connector dialog.
        setPiiEnabled(true);
        setPiiDetectors(PII_DEFAULT_DETECTORS);
        setPiiRedaction("mask");
        setPiiFailClosed(true);
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
  }, [installHandle, allowed, t, toast, router]);

  // #1375: a cancelled/failed Slack OAuth consent redirects back with
  // ?slack_error=cancelled|failed (allowlisted by the backend). Surface a
  // notice and strip the param so refresh/back doesn't re-trigger it. Cancel
  // is a user choice (informational toast); anything else is destructive.
  useEffect(() => {
    if (!slackError) return;
    if (slackError === "cancelled") {
      toast({
        title: t("slackCancelledTitle"),
        description: t("slackCancelledDesc"),
      });
    } else {
      toast({
        variant: "destructive",
        title: t("slackFailedTitle"),
        description: t("slackFailedDesc"),
      });
    }
    router.replace("/workspace/integrations/connectors");
  }, [slackError, t, toast, router]);

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
      // #890: build a valid pii_guardrail_config. When disabled, send an
      // empty detectors list (backend only requires non-empty when enabled);
      // when enabled, the UI guarantees ≥1 detector (submit is blocked
      // otherwise) so the {enabled:true, detectors:[]} 422 can't occur.
      const result = await createConnector({
        connector_type: "slack",
        app_key: pending.app_key,
        resource_id: toResourceId(pending.team_id),
        display_name: displayName || undefined,
        auto_create_context_name: contextName || undefined,
        slack_install_handle: installHandle,
        pii_guardrail_config: {
          enabled: piiEnabled,
          detectors: piiEnabled ? piiDetectors : [],
          redaction: piiRedaction,
          // Derive the recognizer locale from the UI locale so a ja workspace
          // gets ja-aware PII detection instead of English-only.
          locale,
          fail_closed: piiFailClosed,
        },
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
  }, [
    installHandle,
    pending,
    displayName,
    contextName,
    piiEnabled,
    piiDetectors,
    piiRedaction,
    piiFailClosed,
    locale,
    router,
    reload,
  ]);

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

  const handleVisionEnabledChange = useCallback(
    async (connector: WorkspaceConnectorSummary, enabled: boolean) => {
      setRuntimeSaving(connector.connector_id);
      try {
        // Pass the snapshot's config_version so a concurrent admin change
        // 409s (and we reload) instead of being silently overwritten by
        // this full-document replacement (#1348).
        const result = await updateConnectorRuntime(
          connector.connector_id,
          {
            ...connector.runtime,
            vision_enabled: enabled,
          },
          connector.config_version,
        );
        setConnectors(
          (current) =>
            current?.map((item) =>
              item.connector_id === connector.connector_id
                ? {
                    ...item,
                    runtime: result.runtime,
                    config_version: result.config_version,
                  }
                : item,
            ) ?? null,
        );
        toast({ title: t("runtimeUpdated") });
      } catch (err) {
        toast({
          variant: "destructive",
          title: t("runtimeUpdateFailed"),
          description: err instanceof Error ? err.message : String(err),
        });
        // A 409 (stale snapshot) or any failure leaves our list stale —
        // refetch so the switch reflects the server state.
        void reload();
      } finally {
        setRuntimeSaving(null);
      }
    },
    [t, toast],
  );

  const handleManualCreate = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!manualAppKey || !manualTeamId || !manualBotToken) return;
      setManualSubmitting(true);
      setManualError(null);
      try {
        const resourceId = toResourceId(`${manualAppKey}-${manualTeamId}`);
        const app = availableApps?.find(
          (candidate) => candidate.app_key === manualAppKey,
        );
        const result = await createConnector({
          connector_type: "slack",
          app_key: manualAppKey,
          resource_id: resourceId,
          display_name: app
            ? `${app.display_name} / ${manualTeamId}`
            : manualTeamId,
          auto_create_context_name: resourceId,
          external_team_id: manualTeamId,
          oauth_tokens: { bot_token: manualBotToken },
          pii_guardrail_config: {
            enabled: true,
            detectors: PII_DEFAULT_DETECTORS,
            redaction: "mask",
            locale,
            fail_closed: true,
          },
        });
        setManualTeamId("");
        setManualBotToken("");
        setCreated(result);
        await reload();
      } catch (err) {
        setManualError(err instanceof Error ? err.message : String(err));
      } finally {
        setManualSubmitting(false);
      }
    },
    [availableApps, locale, manualAppKey, manualBotToken, manualTeamId, reload],
  );

  // Resolve loading before role gating to avoid a flash of the admin UI
  // before the workspace role is known. All hooks above run unconditionally
  // (these early returns are after every hook call).
  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} description={t("description")} />
        <TableLoadingState rows={3} />
      </PageContainer>
    );
  }

  // Distinguish "no workspace selected" from "wrong role" so a brand-new
  // account with zero workspaces doesn't see a misleading role banner.
  if (!currentWorkspaceId) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} description={t("description")} />
        <ErrorBanner error={t("errors.noWorkspaceSelected")} />
      </PageContainer>
    );
  }

  if (!allowed) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} description={t("description")} />
        <ErrorBanner error={t("errors.forbiddenWorkspace")} />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      <div className="mb-4 flex justify-end">
        <Button onClick={() => (window.location.href = slackInstallUrl())}>
          {t("connectSlack")}
        </Button>
      </div>

      {appsLoadError && !availableApps && <ErrorBanner error={appsLoadError} />}

      {availableApps && availableApps.length > 0 && (
        <form
          onSubmit={handleManualCreate}
          className="mb-6 grid gap-3 rounded-md border p-4 md:grid-cols-4"
        >
          <div className="md:col-span-4">
            <h2 className="font-medium">{t("manualBindTitle")}</h2>
            <p className="text-sm text-muted-foreground">
              {t("manualBindDescription")}
            </p>
          </div>
          {manualError && (
            <Alert variant="destructive" className="md:col-span-4">
              <AlertDescription>{manualError}</AlertDescription>
            </Alert>
          )}
          <Select value={manualAppKey} onValueChange={setManualAppKey}>
            <SelectTrigger aria-label={t("manualAppIdentity")}>
              <SelectValue placeholder={t("manualAppIdentity")} />
            </SelectTrigger>
            <SelectContent>
              {availableApps.map((app) => (
                <SelectItem key={app.app_key} value={app.app_key}>
                  {app.display_name} ({app.app_key})
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Input
            aria-label={t("manualTeamId")}
            placeholder={t("manualTeamId")}
            value={manualTeamId}
            onChange={(event) => setManualTeamId(event.target.value)}
            required
          />
          <Input
            aria-label={t("manualBotToken")}
            placeholder={t("manualBotToken")}
            type="password"
            autoComplete="new-password"
            value={manualBotToken}
            onChange={(event) => setManualBotToken(event.target.value)}
            required
          />
          <Button
            type="submit"
            disabled={
              manualSubmitting ||
              !manualAppKey ||
              !manualTeamId ||
              !manualBotToken
            }
          >
            {t("manualBind")}
          </Button>
        </form>
      )}

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
              <div className="min-w-0">
                <p className="font-medium capitalize">{c.connector_type}</p>
                <p className="text-xs text-muted-foreground">
                  {t("appIdentity", { appKey: c.app_key })}
                </p>
                <p className="text-sm text-muted-foreground">
                  {c.context_id
                    ? t("contextBound", { id: c.context_id })
                    : t("contextNotReady")}
                </p>
                {/* #893: connector_id is non-secret — show it in the list
                    (support / log correlation / CLI target) with a copy button,
                    instead of in the one-time reveal. */}
                <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
                  <span className="font-mono break-all">
                    {t("connectorIdLabel", { id: c.connector_id })}
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={t("copyConnectorId")}
                    onClick={() =>
                      handleCopy(c.connector_id, `cid-${c.connector_id}`)
                    }
                  >
                    {isCopied(`cid-${c.connector_id}`) ? (
                      <Check className="h-4 w-4" />
                    ) : (
                      <Copy className="h-4 w-4" />
                    )}
                  </Button>
                </div>
              </div>
              <div className="flex items-center gap-4">
                <label className="flex items-center gap-2 text-sm">
                  <span>{t("visionEnabled")}</span>
                  {runtimeSaving === c.connector_id && (
                    <InlineSpinner aria-hidden="true" />
                  )}
                  <Switch
                    checked={c.runtime?.vision_enabled ?? true}
                    disabled={
                      c.runtime == null || runtimeSaving === c.connector_id
                    }
                    onCheckedChange={(enabled) =>
                      void handleVisionEnabledChange(c, enabled)
                    }
                    aria-label={t("visionEnabledFor", {
                      id: c.connector_id,
                    })}
                  />
                </label>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setToDelete(c)}
                  aria-label={tCommon("delete")}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
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

            {/* #890: PII guardrail configuration */}
            <div className="border-t pt-4">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{t("piiTitle")}</p>
                  <p className="text-xs text-muted-foreground">
                    {t("piiDesc")}
                  </p>
                </div>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={piiEnabled}
                    onChange={(e) => setPiiEnabled(e.target.checked)}
                  />
                  {t("piiEnabled")}
                </label>
              </div>

              {piiEnabled && (
                <div className="mt-3 space-y-3">
                  <div>
                    <p className="mb-1 text-xs font-medium">
                      {t("piiDetectors")}
                    </p>
                    <div className="grid grid-cols-2 gap-1">
                      {PII_DETECTORS.map((d) => (
                        <label
                          key={d}
                          className="flex items-center gap-2 text-sm"
                        >
                          <input
                            type="checkbox"
                            checked={piiDetectors.includes(d)}
                            onChange={(e) =>
                              setPiiDetectors((prev) =>
                                e.target.checked
                                  ? [...prev, d]
                                  : prev.filter((x) => x !== d),
                              )
                            }
                          />
                          <span className="font-mono text-xs">{d}</span>
                        </label>
                      ))}
                    </div>
                    {piiDetectors.length === 0 && (
                      <p className="mt-1 text-xs text-destructive">
                        {t("piiDetectorsRequired")}
                      </p>
                    )}
                  </div>
                  <div>
                    <label
                      htmlFor="conn-pii-redaction"
                      className="mb-1 block text-xs font-medium"
                    >
                      {t("piiRedaction")}
                    </label>
                    <Select
                      value={piiRedaction}
                      onValueChange={(v) => setPiiRedaction(v as PiiRedaction)}
                    >
                      <SelectTrigger id="conn-pii-redaction" className="h-9">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {PII_REDACTION_MODES.map((m) => (
                          <SelectItem key={m} value={m}>
                            {t(`piiRedaction_${m}`)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={piiFailClosed}
                      onChange={(e) => setPiiFailClosed(e.target.checked)}
                    />
                    {t("piiFailClosed")}
                  </label>
                </div>
              )}
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
              disabled={
                submitting ||
                !contextName ||
                (piiEnabled && piiDetectors.length === 0)
              }
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
            {/* #893: Model B users do nothing after registration — the worker
                fetches credentials server-to-server. Don't frame secrets as
                "save these now". */}
            <AlertDialogDescription>
              {t("createdDescModelB")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-3 text-sm">
            {/* #893: developer/CLI credentials collapsed by default — only
                needed for manual curl/CLI testing or a self-hosted worker. */}
            <details className="rounded-md border p-3">
              <summary className="cursor-pointer text-sm font-medium">
                {t("devDisclosureTitle")}
              </summary>
              <div className="mt-3 space-y-3">
                <p className="text-xs text-muted-foreground">
                  {t("devDisclosureNote")}
                </p>
                {created?.token && (
                  <div>
                    <div className="flex items-center justify-between">
                      <p className="font-medium">{t("resourceToken")}</p>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        aria-label={t("copyResourceToken")}
                        onClick={() =>
                          handleCopy(created.token, "reveal-token")
                        }
                      >
                        {isCopied("reveal-token") ? (
                          <Check className="h-4 w-4" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                    <code className="block break-all text-xs">
                      {created.token}
                    </code>
                  </div>
                )}
                {created?.token && created?.resource_id && (
                  <div>
                    <p className="mb-1 font-medium">{t("curlSampleTitle")}</p>
                    <pre className="overflow-x-auto rounded bg-muted p-2 text-xs">
                      {curlSample(created.resource_id, created.token)}
                    </pre>
                  </div>
                )}
                {created?.kmc_api_key && (
                  <div>
                    <div className="flex items-center justify-between">
                      <p className="font-medium">{t("kmcApiKey")}</p>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        aria-label={t("copyKmcApiKey")}
                        onClick={() =>
                          handleCopy(created.kmc_api_key!, "reveal-kmc")
                        }
                      >
                        {isCopied("reveal-kmc") ? (
                          <Check className="h-4 w-4" />
                        ) : (
                          <Copy className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                    <p className="mb-1 text-xs text-muted-foreground">
                      {t("kmcApiKeyNote")}
                    </p>
                    <code className="block break-all text-xs">
                      {created.kmc_api_key}
                    </code>
                  </div>
                )}
              </div>
            </details>
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
