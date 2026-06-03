"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Check, Copy, Plug, Trash2 } from "lucide-react";

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
import { API_BASE_URL } from "@/lib/api/base";
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

// #890: Presidio recognizer names offered in the PII config UI. Kept in sync
// with the worker's recognizer set; the backend validates only that detectors
// is a non-empty list of non-blank strings (PiiGuardrailConfig).
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

/** Inline copy-to-clipboard button with a transient checkmark. */
function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label={label}
      onClick={() => {
        void navigator.clipboard.writeText(value);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
    </Button>
  );
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
      // #890: build a valid pii_guardrail_config. When disabled, send an
      // empty detectors list (backend only requires non-empty when enabled);
      // when enabled, the UI guarantees ≥1 detector (submit is blocked
      // otherwise) so the {enabled:true, detectors:[]} 422 can't occur.
      const result = await createConnector({
        connector_type: "slack",
        resource_id: toResourceId(pending.team_id),
        display_name: displayName || undefined,
        auto_create_context_name: contextName || undefined,
        slack_install_handle: installHandle,
        pii_guardrail_config: {
          enabled: piiEnabled,
          detectors: piiEnabled ? piiDetectors : [],
          redaction: piiRedaction,
          locale: "en",
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
              <div className="min-w-0">
                <p className="font-medium capitalize">{c.connector_type}</p>
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
                  <CopyButton
                    value={c.connector_id}
                    label={t("copyConnectorId")}
                  />
                </div>
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
                    <select
                      id="conn-pii-redaction"
                      className="h-9 w-full rounded-md border bg-transparent px-2 text-sm"
                      value={piiRedaction}
                      onChange={(e) =>
                        setPiiRedaction(e.target.value as PiiRedaction)
                      }
                    >
                      {PII_REDACTION_MODES.map((m) => (
                        <option key={m} value={m}>
                          {t(`piiRedaction_${m}`)}
                        </option>
                      ))}
                    </select>
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
                      <CopyButton
                        value={created.token}
                        label={t("copyResourceToken")}
                      />
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
                      <CopyButton
                        value={created.kmc_api_key}
                        label={t("copyKmcApiKey")}
                      />
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
