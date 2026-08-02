"use client";

import {
  FormEvent,
  ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Copy,
  Info,
  Pencil,
  Plug,
  Trash2,
} from "lucide-react";

import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import {
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { Badge, badgeVariants } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils/cn";
import { formatDateTime, formatRelativeTime } from "@/lib/utils/datetime";
import { CONNECTOR_PROVIDERS } from "@/lib/connectors/providers";
import { useToast } from "@/hooks/use-toast";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useConsumeSearchParams } from "@/hooks/useConsumeSearchParams";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { ChannelPicker, parseChannelIds } from "./ChannelPicker";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { API_BASE_URL } from "@/lib/api/base";
import { getContexts, type Context } from "@/lib/api/contexts";
import {
  connectorDisplayName,
  connectorReadiness,
  createConnector,
  deleteConnector,
  getSlackPendingInstall,
  listAvailableWorkerApps,
  listConnectors,
  slackInstallUrl,
  updateConnectorRuntime,
  updateConnectorSettings,
  type AvailableWorkerApp,
  type CreateConnectorResponse,
  type SlackPendingInstall,
  type UpdateConnectorSettingsRequest,
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

// #1388: one status chip shape for the settings-dialog sections and the
// list-row aggregate badge — set/unset color language defined once.
function StatusChip({
  set,
  setLabel,
  unsetLabel,
}: {
  set: boolean;
  setLabel: string;
  unsetLabel: string;
}) {
  return (
    <Badge variant={set ? "secondary" : "outline"}>
      {set ? setLabel : unsetLabel}
    </Badge>
  );
}

// #1389: a missing vend-setting rendered as an affordance — a badge-shaped
// button that opens the settings dialog at the relevant section.
function MissingBadgeButton({
  onClick,
  label,
  children,
}: {
  onClick: () => void;
  label: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className={cn(
        badgeVariants({ variant: "outline" }),
        "cursor-pointer hover:bg-accent",
      )}
      onClick={onClick}
      aria-label={label}
    >
      {children}
    </button>
  );
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

  // #1426: managed (hosted SaaS) mode. When true the shared worker/bridge
  // provides the pre-compile LLM and only OAuth is offered, so hide the BYO
  // "link existing Slack app" form and stop treating a missing per-connector
  // LLM as un-vendable. Missing flag → false (OSS/self-host default-off).
  const features = useSystemFeatures();
  const managedConnectors = features?.managed_connectors === true;

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
  // #1409: write-target selection. "existing" binds the connector to an
  // already-existing context (send context_id); "new" auto-creates one (send
  // auto_create_context_name). Exactly one is sent — the backend rejects both.
  const [contextMode, setContextMode] = useState<"existing" | "new">("new");
  const [availableContexts, setAvailableContexts] = useState<Context[]>([]);
  const [selectedContextId, setSelectedContextId] = useState("");
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
  // #1471: a SET of in-flight connector ids, not a single slot. With one slot
  // a second row's save overwrites the first on ACQUIRE, and whichever request
  // lands first then releases it — re-enabling controls for a request still in
  // flight. Guarding only the release is not enough; the acquire races too.
  const [runtimeSaving, setRuntimeSaving] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const beginRuntimeSave = useCallback((connectorId: string) => {
    setRuntimeSaving((current) => new Set(current).add(connectorId));
  }, []);
  const endRuntimeSave = useCallback((connectorId: string) => {
    setRuntimeSaving((current) => {
      const next = new Set(current);
      next.delete(connectorId);
      return next;
    });
  }, []);

  // #1376: vend-settings editor (channels / locale / LLM binding). The LLM
  // credential fields are write-only: never prefilled, sent only when the
  // admin fills all three (or ticks the explicit clear).
  const [settingsFor, setSettingsFor] =
    useState<WorkspaceConnectorSummary | null>(null);
  const [chText, setChText] = useState("");
  const [localeSel, setLocaleSel] = useState<"default" | "en" | "ja">(
    "default",
  );
  const [litellmKey, setLitellmKey] = useState("");
  // #1471: runtime.memory_link_template. Lives in the settings dialog for
  // proximity, but saves through the RUNTIME endpoint, not the vend-settings
  // one — see handleSettingsSave for why the two writes must be sequenced.
  const [memoryLinkTemplate, setMemoryLinkTemplate] = useState("");
  const [llmProvider, setLlmProvider] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  // #1388: deleting the stored LLM bundle is an explicit destructive action
  // (confirm dialog + immediate PATCH), not a latched save-time toggle.
  const [llmDeleteConfirm, setLlmDeleteConfirm] = useState(false);
  const [llmDeleting, setLlmDeleting] = useState(false);
  // Focus target after the delete confirm closes: the delete button (the
  // Radix focus-return trigger) unmounts once the PATCH resolves, and the
  // footer buttons are disabled while it is in flight, so the dialog
  // container (tabIndex=-1) is the only stable target.
  const settingsContentRef = useRef<HTMLDivElement | null>(null);
  // #1389: a missing row badge opens the settings dialog at its section —
  // the requested section's input takes the dialog's initial focus.
  const [settingsFocus, setSettingsFocus] = useState<"channels" | "llm" | null>(
    null,
  );
  const channelsInputRef = useRef<HTMLInputElement | null>(null);
  const llmProviderInputRef = useRef<HTMLInputElement | null>(null);

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
        // #1409: load the workspace's contexts so the operator can bind this
        // connector to an existing context instead of always minting a new
        // one (the cause of duplicate slack-* contexts + the silent same-name
        // create failure). The server re-validates workspace membership on the
        // submitted context_id, so listing the workspace's own contexts is
        // sufficient. A load failure degrades to create-new only — it must
        // never block the install dialog.
        let contexts: Context[] = [];
        try {
          const ctxResp = await getContexts();
          contexts = ctxResp.contexts;
        } catch {
          contexts = [];
        }
        if (cancelled) return;
        setAvailableContexts(contexts);
        // Default to connecting to an existing context when any exist.
        setContextMode(contexts.length > 0 ? "existing" : "new");
        setSelectedContextId(contexts[0]?.id ?? "");
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
    // t / toast / router are stable (next-intl memoizes the translator, the
    // Next router is a singleton). Keeping them out of the deps means this
    // effect initializes the dialog exactly once per install handle instead
    // of re-fetching + resetting the form (mode, name, PII) on every unrelated
    // re-render — which would clobber the operator's write-target choice (#1409).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [installHandle, allowed]);

  // #1375/#1381: a cancelled/failed/expired Slack OAuth consent redirects
  // back with ?slack_error=cancelled|failed|expired (allowlisted by the
  // backend). Surface a notice, then the hook strips the param so
  // refresh/back doesn't re-trigger it (#1382). Cancel is a user choice
  // (informational toast); expired is retryable (say so); anything else is
  // destructive. Gated on `allowed` like the slack_install effect — only
  // admins can run the install flow, so only they get the outcome notice.
  useConsumeSearchParams(
    (params) => {
      const slackError = params.get("slack_error");
      if (!slackError) return false;
      if (slackError === "cancelled") {
        toast({
          title: t("slackCancelledTitle"),
          description: t("slackCancelledDesc"),
        });
      } else if (slackError === "expired") {
        toast({
          variant: "destructive",
          title: t("slackExpiredTitle"),
          description: t("slackExpiredDesc"),
        });
      } else {
        toast({
          variant: "destructive",
          title: t("slackFailedTitle"),
          description: t("slackFailedDesc"),
        });
      }
      return true;
    },
    { enabled: allowed, cleanUrl: "/workspace/integrations/connectors" },
  );

  const openSettings = useCallback(
    (c: WorkspaceConnectorSummary, focus: "channels" | "llm" | null = null) => {
      setSettingsFor(c);
      setSettingsFocus(focus);
      setChText((c.channel_ids ?? []).join(", "));
      setLocaleSel(
        c.locale === "en" || c.locale === "ja" ? c.locale : "default",
      );
      setLitellmKey(c.litellm_virtual_key_id ?? "");
      setMemoryLinkTemplate(c.runtime?.memory_link_template ?? "");
      setLlmProvider("");
      setLlmModel("");
      setLlmApiKey("");
      setSettingsError(null);
    },
    [],
  );

  // #1388: after a PATCH failure (usually a 409 from a stale snapshot),
  // re-sync the dialog snapshot from the server so a retry rides the fresh
  // config_version instead of looping on the same conflict. Form drafts
  // (chText etc.) live in separate state and are deliberately untouched.
  const resyncSettingsSnapshot = useCallback(async () => {
    try {
      const fetched = await listConnectors();
      // config_version is monotonic, so gate every merge on it: a slow
      // resync response must never clobber a newer local patch (e.g. a
      // delete that succeeded while this fetch was in flight).
      const newerOnly = (item: WorkspaceConnectorSummary) => {
        const fresh = fetched.find((c) => c.connector_id === item.connector_id);
        return fresh && fresh.config_version > item.config_version
          ? fresh
          : item;
      };
      setConnectors((current) => (current ? current.map(newerOnly) : fetched));
      setSettingsFor((prev) => (prev ? newerOnly(prev) : prev));
    } catch {
      // Best-effort: keep the stale snapshot; close-and-reopen still works.
    }
  }, []);

  const handleSettingsSave = useCallback(async () => {
    if (!settingsFor) return;
    setSettingsError(null);

    const patch: UpdateConnectorSettingsRequest = {};
    const parsedChannels = parseChannelIds(chText);
    const newChannels = parsedChannels.length ? parsedChannels : null;
    const oldChannels = settingsFor.channel_ids?.length
      ? settingsFor.channel_ids
      : null;
    // Order-insensitive compare: channel selection is a set, and a
    // reorder-only save must not bump config_version / refetch the worker.
    const sortedJson = (ids: string[] | null) =>
      ids ? JSON.stringify([...ids].sort()) : "null";
    if (sortedJson(newChannels) !== sortedJson(oldChannels)) {
      patch.channel_ids = newChannels;
    }
    // Compare select STATE against its initial mapping (not the raw stored
    // value) so a stored locale this 3-option select cannot represent is
    // never silently cleared by an unrelated save.
    const initialLocaleSel =
      settingsFor.locale === "en" || settingsFor.locale === "ja"
        ? settingsFor.locale
        : "default";
    if (localeSel !== initialLocaleSel) {
      patch.locale = localeSel === "default" ? null : localeSel;
    }
    const newLitellmKey = litellmKey.trim() || null;
    if (newLitellmKey !== (settingsFor.litellm_virtual_key_id ?? null)) {
      patch.litellm_virtual_key_id = newLitellmKey;
    }
    if (llmProvider.trim() || llmModel.trim() || llmApiKey.trim()) {
      // Provider and model are the backend's universal minimum
      // (_validate_llm_config); api_key is provider-dependent (local
      // Ollama has none) and is omitted from the bundle when empty.
      if (!(llmProvider.trim() && llmModel.trim())) {
        setSettingsError(t("llmIncomplete"));
        return;
      }
      patch.llm_config = {
        provider: llmProvider.trim(),
        model: llmModel.trim(),
        ...(llmApiKey.trim() ? { api_key: llmApiKey.trim() } : {}),
      };
    }
    // #1471: memory_link_template is a RUNTIME field, so it rides the other
    // endpoint. Empty input clears the override to null rather than storing "".
    const newLinkTemplate = memoryLinkTemplate.trim() || null;
    const linkTemplateChanged =
      newLinkTemplate !== (settingsFor.runtime?.memory_link_template ?? null);

    if (Object.keys(patch).length === 0 && !linkTemplateChanged) {
      setSettingsError(t("noChanges"));
      return;
    }

    setSettingsSaving(true);
    try {
      // Both endpoints consume AND bump config_version, so writing one
      // invalidates the snapshot the other would send. Chain them: take the
      // fresh version out of the first response and hand it to the second.
      // Doing them independently from the same snapshot 409s the second write.
      let version = settingsFor.config_version;
      if (Object.keys(patch).length > 0) {
        const settingsResult = await updateConnectorSettings(
          settingsFor.connector_id,
          patch,
          // Snapshot version rides along as the optimistic-concurrency guard
          // (server 409s on staleness instead of silently reverting).
          version,
        );
        version = settingsResult.config_version;
      }
      if (linkTemplateChanged) {
        // Spread the CURRENT runtime: this endpoint is a complete normalized
        // replacement, so a partial body silently resets every other tuned
        // field (buffer, flush, lifecycle, …) to worker defaults.
        await updateConnectorRuntime(
          settingsFor.connector_id,
          { ...settingsFor.runtime, memory_link_template: newLinkTemplate },
          version,
        );
      }
      toast({ title: t("settingsSaved") });
      setSettingsFor(null);
      void reload();
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : String(err));
      // Recover from a stale-snapshot 409: without the re-sync every retry
      // re-sends the same stale expected_config_version and loops.
      void resyncSettingsSnapshot();
    } finally {
      setSettingsSaving(false);
    }
  }, [
    settingsFor,
    chText,
    localeSel,
    litellmKey,
    memoryLinkTemplate,
    llmProvider,
    llmModel,
    llmApiKey,
    t,
    toast,
    reload,
    resyncSettingsSnapshot,
  ]);

  // #1388: immediate destructive PATCH behind an explicit confirm. On
  // success the dialog stays open on a refreshed snapshot — the returned
  // config_version replaces the stale one so a follow-up save doesn't 409 —
  // and the list row is patched in place from the response (same pattern as
  // the vision toggle) instead of refetching both list endpoints.
  const handleLlmDelete = useCallback(async () => {
    if (!settingsFor) return;
    setLlmDeleting(true);
    setSettingsError(null);
    try {
      const result = await updateConnectorSettings(
        settingsFor.connector_id,
        { llm_config: null },
        settingsFor.config_version,
      );
      toast({ title: t("llmDeleted") });
      const applyResult = (item: WorkspaceConnectorSummary) => ({
        ...item,
        channel_ids: result.channel_ids,
        litellm_virtual_key_id: result.litellm_virtual_key_id,
        llm_config_present: result.llm_config_present,
        locale: result.locale,
        config_version: result.config_version,
      });
      setSettingsFor((prev) =>
        prev && prev.connector_id === settingsFor.connector_id
          ? applyResult(prev)
          : prev,
      );
      setConnectors(
        (current) =>
          current?.map((item) =>
            item.connector_id === settingsFor.connector_id
              ? applyResult(item)
              : item,
          ) ?? null,
      );
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : String(err));
      void resyncSettingsSnapshot();
    } finally {
      setLlmDeleting(false);
    }
  }, [settingsFor, t, toast, resyncSettingsSnapshot]);

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
        // #1409: send EXACTLY ONE write-target field. Existing mode binds the
        // chosen context_id (backend re-validates workspace membership);
        // create-new mode auto-creates. Sending both is a backend
        // ValidationError, so the branches are mutually exclusive.
        ...(contextMode === "existing"
          ? { context_id: selectedContextId }
          : { auto_create_context_name: contextName || undefined }),
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
    contextMode,
    selectedContextId,
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
      beginRuntimeSave(connector.connector_id);
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
        endRuntimeSave(connector.connector_id);
      }
    },
    [t, toast, reload, beginRuntimeSave, endRuntimeSave],
  );

  const handleManualCreate = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!manualAppKey || !manualTeamId || !manualBotToken) return;
      // #1389: client-side shape checks (UX only — the backend stays the
      // authority). A bot token is always `xoxb-`-prefixed; pasting a user
      // token (xoxp-) or an app token (xapp-) is the common first-run
      // mistake. Team IDs are uppercase alphanumeric, T-prefixed — or
      // E-prefixed for an Enterprise Grid org install.
      if (!manualBotToken.startsWith("xoxb-")) {
        setManualError(t("manualBotTokenInvalid"));
        return;
      }
      const teamId = manualTeamId.trim();
      if (!/^[TE][A-Z0-9]+$/.test(teamId)) {
        setManualError(t("manualTeamIdInvalid"));
        return;
      }
      setManualSubmitting(true);
      setManualError(null);
      try {
        // Submit the trimmed ID — event dispatch matches external_team_id
        // exactly, so a pasted trailing space/newline would create a
        // connector that silently never receives events.
        const resourceId = toResourceId(`${manualAppKey}-${teamId}`);
        const app = availableApps?.find(
          (candidate) => candidate.app_key === manualAppKey,
        );
        const result = await createConnector({
          connector_type: "slack",
          app_key: manualAppKey,
          resource_id: resourceId,
          display_name: app ? `${app.display_name} / ${teamId}` : teamId,
          auto_create_context_name: resourceId,
          external_team_id: teamId,
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
    [
      availableApps,
      locale,
      manualAppKey,
      manualBotToken,
      manualTeamId,
      reload,
      t,
    ],
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

  // #1388: stored-state readiness for the settings dialog summary (not the
  // form draft — the dialog exists to repair un-vendable connectors, #1376).
  const settingsReadiness = settingsFor
    ? connectorReadiness(settingsFor, { llmRequired: !managedConnectors })
    : null;

  // #1409: the create dialog's submit needs a valid write target for the
  // active mode — a selected context_id when connecting to an existing one,
  // a non-empty name when auto-creating.
  const createBindingReady =
    contextMode === "existing" ? !!selectedContextId : !!contextName;

  // #1409/#1399: the create-new context-name field renders in two spots
  // (create-new mode when the workspace has contexts, and the zero-contexts
  // fallback). One definition keeps the visible <label htmlFor> (the #1399
  // a11y convention) + help copy from drifting; only one branch renders per
  // pass, so the shared id="conn-context-name" never collides in the DOM.
  const contextNameField = (
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
  );

  // #1399: the LLM inputs render in two spots — directly when nothing is
  // stored, inside the replace fold when a config is present. One definition
  // keeps the label/id wiring and help copy from drifting between the two.
  const llmFieldsBlock = (
    <>
      <p className="text-xs text-muted-foreground">{t("llmHelp")}</p>
      <div>
        <label
          htmlFor="conn-settings-llm-provider"
          className="mb-1 block text-sm font-medium"
        >
          {t("llmProvider")}
        </label>
        <Input
          id="conn-settings-llm-provider"
          ref={llmProviderInputRef}
          placeholder={t("llmProviderPlaceholder")}
          value={llmProvider}
          onChange={(e) => setLlmProvider(e.target.value)}
        />
      </div>
      <div>
        <label
          htmlFor="conn-settings-llm-model"
          className="mb-1 block text-sm font-medium"
        >
          {t("llmModel")}
        </label>
        <Input
          id="conn-settings-llm-model"
          placeholder={t("llmModelPlaceholder")}
          value={llmModel}
          onChange={(e) => setLlmModel(e.target.value)}
        />
      </div>
      <div>
        <label
          htmlFor="conn-settings-llm-apikey"
          className="mb-1 block text-sm font-medium"
        >
          {t("llmApiKey")}
        </label>
        <Input
          id="conn-settings-llm-apikey"
          type="password"
          value={llmApiKey}
          onChange={(e) => setLlmApiKey(e.target.value)}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        {t("llmApiKeyOptionalHelp")}
      </p>
    </>
  );

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      {/* #1389: provider picker rendered from the CONNECTOR_PROVIDERS
          descriptor — Slack live, Discord/Teams disabled coming-soon — so
          Slack-hardcoded JSX stops multiplying (#1390). */}
      <div className="mb-4 flex flex-wrap justify-end gap-2">
        {CONNECTOR_PROVIDERS.map((provider) => (
          <Button
            key={provider.key}
            variant={provider.enabled ? "default" : "outline"}
            disabled={!provider.enabled}
            onClick={
              provider.enabled
                ? () => {
                    // Routing lives in the descriptor: a provider enabled
                    // without its own flow yields a no-op, never another
                    // provider's OAuth screen.
                    const url = provider.installUrl?.();
                    if (url) window.location.href = url;
                  }
                : undefined
            }
          >
            <provider.icon className="h-4 w-4" aria-hidden="true" />
            {provider.enabled
              ? t("connectProvider", { name: provider.name })
              : `${provider.name} — ${t("comingSoon")}`}
          </Button>
        ))}
      </div>

      {appsLoadError && !availableApps && <ErrorBanner error={appsLoadError} />}

      {/* #1389: the manual-bind form is the advanced/secondary path — folded
          by default so first-run users see explanation + one primary CTA.
          #1426: on managed (hosted SaaS) it is hidden entirely — BYO apps are a
          self-host affordance; SaaS tenants use OAuth only. */}
      {!managedConnectors && availableApps && availableApps.length > 0 && (
        <details className="mb-6 rounded-md border">
          <summary className="cursor-pointer p-4 font-medium">
            {t("manualBindTitle")}
          </summary>
          <form
            onSubmit={handleManualCreate}
            className="grid gap-3 p-4 pt-0 md:grid-cols-4"
          >
            <div className="md:col-span-4">
              <p className="text-sm text-muted-foreground">
                {t("manualBindIntro")}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
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
            <div>
              <Input
                aria-label={t("manualTeamId")}
                placeholder={t("manualTeamId")}
                value={manualTeamId}
                onChange={(event) => setManualTeamId(event.target.value)}
                required
              />
              <p className="mt-1 text-xs text-muted-foreground">
                {t("manualTeamIdHelp")}
              </p>
            </div>
            <div>
              <Input
                aria-label={t("manualBotToken")}
                placeholder={t("manualBotToken")}
                type="password"
                autoComplete="new-password"
                value={manualBotToken}
                onChange={(event) => setManualBotToken(event.target.value)}
                required
              />
              <p className="mt-1 text-xs text-muted-foreground">
                {t("manualBotTokenHelp")}
              </p>
            </div>
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
        </details>
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
          actionLabel={t("connectProvider", { name: "Slack" })}
          onAction={() => (window.location.href = slackInstallUrl())}
        />
      ) : (
        // One provider for the whole list so Radix's shared skip-delay
        // grouping works across rows (per-row providers would isolate it).
        <TooltipProvider delayDuration={200}>
          <ul className="divide-y rounded-md border">
            {connectors.map((c) => {
              // #1388/#1389: one readiness rule for the aggregate badge and
              // the per-part chips — shared with the settings dialog.
              const readiness = connectorReadiness(c, {
                llmRequired: !managedConnectors,
              });
              // #1471: derive the field's value ONCE. Reading the draft-or-
              // server fallback separately in the input and in the Save
              // button's disabled test is how "unchanged" silently became
              // "draft is empty", leaving Save enabled on first render.
              return (
                <li
                  key={c.connector_id}
                  className="flex items-center justify-between p-4"
                >
                  <div className="min-w-0">
                    {/* #1389: human names first — resource label, then the
                      platform team id, then the capitalized type. */}
                    <p className="font-medium">{connectorDisplayName(c)}</p>
                    <p className="text-xs text-muted-foreground">
                      {t("appIdentity", { appKey: c.app_key })}
                    </p>
                    <div className="flex items-center gap-1 text-sm text-muted-foreground">
                      <span>
                        {c.context_name
                          ? t("contextBoundName", { name: c.context_name })
                          : c.context_id
                            ? t("contextBound", { id: c.context_id })
                            : t("contextNotReady")}
                      </span>
                      {/* UUID demoted behind the copy affordance (#1389). */}
                      {c.context_id && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          aria-label={t("copyContextId")}
                          onClick={() =>
                            handleCopy(c.context_id!, `ctx-${c.connector_id}`)
                          }
                        >
                          {isCopied(`ctx-${c.connector_id}`) ? (
                            <Check className="h-4 w-4" />
                          ) : (
                            <Copy className="h-4 w-4" />
                          )}
                        </Button>
                      )}
                    </div>
                    {/* #1449: ingest outcome. The 2026-07-21..27 outage was
                      invisible from every screen while this very date sat in
                      the database. Stated as fact and never coloured — no
                      traffic is a normal state for a quiet workspace, and a row
                      that is permanently red gets ignored just like an alert
                      that fires permanently. */}
                    <p
                      className="mt-1 text-xs text-muted-foreground"
                      // Relative time scans fast ("6 days ago"); the exact UTC
                      // timestamp is what an operator lines up against logs and
                      // an outage window, so keep both (review finding).
                      title={
                        c.last_memory_at
                          ? formatDateTime(c.last_memory_at, "UTC", locale)
                          : undefined
                      }
                    >
                      {c.last_memory_at
                        ? t("ingestLastWrite", {
                            when: formatRelativeTime(c.last_memory_at, locale),
                          })
                        : t("ingestNeverWritten")}
                      {" · "}
                      {t("ingestLast7d", { count: c.memories_last_7d })}
                      {c.ingest_context_shared && (
                        <> · {t("ingestSharedContext")}</>
                      )}
                    </p>
                    {/* #893: connector_id is non-secret — show it in the list
                      (support / log correlation / CLI target) with a copy
                      button, instead of in the one-time reveal. */}
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
                    {/* #1376/#1389: vend-settings status badges — the aggregate
                      readiness first, then per-part chips. Missing parts are
                      buttons that open the settings dialog at that section. */}
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <StatusChip
                        set={readiness.ready}
                        setLabel={t("runningBadge")}
                        unsetLabel={t("needsSetupBadge")}
                      />
                      {readiness.missingChannels ? (
                        <MissingBadgeButton
                          onClick={() => openSettings(c, "channels")}
                          label={t("fixChannels")}
                        >
                          {t("channelsNone")}
                        </MissingBadgeButton>
                      ) : (
                        <Badge variant="secondary">
                          {t("channelsCount", {
                            count: c.channel_ids?.length ?? 0,
                          })}
                        </Badge>
                      )}
                      {readiness.missingLlm ? (
                        <MissingBadgeButton
                          onClick={() => openSettings(c, "llm")}
                          label={t("fixLlm")}
                        >
                          {t("llmNotBound")}
                        </MissingBadgeButton>
                      ) : (
                        <Badge variant="secondary">{t("llmBound")}</Badge>
                      )}
                      <Badge variant="outline">
                        {c.locale === "en" || c.locale === "ja"
                          ? c.locale
                          : t("localeDefault")}
                      </Badge>
                    </div>
                  </div>
                  <div className="flex items-center gap-4">
                    {/* NOT a <label>: it would wrap two labelable elements
                      (the tooltip button + the Switch) and the label's
                      activation target would be the tooltip button, so
                      clicking the text could never toggle the switch. The
                      Switch carries its own aria-label. */}
                    <div className="flex items-center gap-2 text-sm">
                      <span>{t("visionEnabled")}</span>
                      {/* #1389: first-run users can't tell what the toggle
                        does — say it on demand. */}
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            aria-label={t("visionTooltipLabel")}
                            className="text-muted-foreground"
                          >
                            <Info className="h-3.5 w-3.5" aria-hidden="true" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>{t("visionTooltip")}</TooltipContent>
                      </Tooltip>
                      {runtimeSaving.has(c.connector_id) && (
                        <InlineSpinner aria-hidden="true" />
                      )}
                      <Switch
                        checked={c.runtime?.vision_enabled ?? true}
                        disabled={
                          c.runtime == null || runtimeSaving.has(c.connector_id)
                        }
                        onCheckedChange={(enabled) =>
                          void handleVisionEnabledChange(c, enabled)
                        }
                        aria-label={t("visionEnabledFor", {
                          id: c.connector_id,
                        })}
                      />
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => openSettings(c)}
                      aria-label={t("editSettings")}
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
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
              );
            })}
          </ul>
        </TooltipProvider>
      )}

      {/* #1376: vend-settings editor */}
      <Dialog
        open={settingsFor !== null}
        // Escape/overlay-close is blocked mid-save AND mid-delete: the error
        // Alert lives in this dialog, so closing while a PATCH is in flight
        // would swallow a failure (#1376 review, extended for #1388).
        onOpenChange={(o) => {
          if (!o && !settingsSaving && !llmDeleting) setSettingsFor(null);
        }}
      >
        <DialogContent
          ref={settingsContentRef}
          tabIndex={-1}
          className="max-h-[85vh] overflow-y-auto"
          // #1389: a missing row badge opens the dialog at its section —
          // steer Radix's initial focus to that section's first input.
          onOpenAutoFocus={(e) => {
            if (settingsFocus === "channels") {
              e.preventDefault();
              channelsInputRef.current?.focus();
            } else if (
              settingsFocus === "llm" &&
              !settingsFor?.llm_config_present
            ) {
              // #1399: mirror the render branch — with a stored config the
              // provider input sits inside the closed replace fold and
              // cannot take focus; leaving Radix's default focus intact is
              // safer than a silent no-op. Unreachable today (fixLlm only
              // renders when the config is missing) but enforced here
              // rather than assumed.
              e.preventDefault();
              llmProviderInputRef.current?.focus();
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>{t("settingsTitle")}</DialogTitle>
            <DialogDescription>{t("settingsDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            {settingsError && (
              <Alert variant="destructive">
                <AlertDescription>{settingsError}</AlertDescription>
              </Alert>
            )}
            {/* #1388: vend-readiness summary — say up front whether the
                connector runs. */}
            {settingsReadiness && (
              <Alert>
                {settingsReadiness.ready ? (
                  <CheckCircle2 className="h-4 w-4" />
                ) : (
                  <AlertTriangle className="h-4 w-4" />
                )}
                <AlertDescription>
                  {settingsReadiness.ready
                    ? t("readySummary")
                    : t("notReadySummary", {
                        missing: [
                          settingsReadiness.missingChannels
                            ? t("missingChannels")
                            : null,
                          settingsReadiness.missingLlm ? t("missingLlm") : null,
                        ]
                          .filter(Boolean)
                          .join(t("missingListSeparator")),
                      })}
                </AlertDescription>
              </Alert>
            )}
            {/* Section 1 — ingest scope */}
            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <p className="text-sm font-medium">{t("sectionIngest")}</p>
                <StatusChip
                  set={!!settingsFor?.channel_ids?.length}
                  setLabel={t("statusSet")}
                  unsetLabel={t("statusUnset")}
                />
              </div>
              <div>
                <p className="mb-1 block text-sm font-medium">
                  {t("channelsLabel")}
                </p>
                {settingsFor && (
                  // #1391: pick from the connector's Slack channels (server-side
                  // proxy); manual-ID entry stays as a fallback lane. Reads/writes
                  // the same chText, so the channel_ids PATCH is unchanged.
                  <ChannelPicker
                    connectorId={settingsFor.connector_id}
                    value={parseChannelIds(chText)}
                    onChange={(ids) => setChText(ids.join(", "))}
                    inputRef={channelsInputRef}
                  />
                )}
              </div>
            </section>
            {/* Section 2 — language */}
            <section className="space-y-2 border-t pt-4">
              <p className="text-sm font-medium">{t("sectionLanguage")}</p>
              <div>
                <label
                  htmlFor="conn-settings-locale"
                  className="mb-1 block text-sm font-medium"
                >
                  {t("localeLabel")}
                </label>
                <Select
                  value={localeSel}
                  onValueChange={(v) =>
                    setLocaleSel(v as "default" | "en" | "ja")
                  }
                >
                  <SelectTrigger
                    id="conn-settings-locale"
                    aria-label={t("localeLabel")}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="default">
                      {t("localeDefault")}
                    </SelectItem>
                    <SelectItem value="en">English</SelectItem>
                    <SelectItem value="ja">日本語</SelectItem>
                  </SelectContent>
                </Select>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t("localeHelp")}
                </p>
              </div>
            </section>
            {/* Section 3 — LLM used for summarization. #1426: on managed
                (hosted SaaS) the shared worker/bridge provides the pre-compile
                LLM, so per-connector LLM config is neither required nor shown —
                a one-line note replaces the whole section. */}
            {managedConnectors ? (
              <section className="space-y-2 border-t pt-4">
                <p className="text-sm font-medium">{t("sectionLlm")}</p>
                <p className="text-sm text-muted-foreground">
                  {t("llmManagedNote")}
                </p>
              </section>
            ) : (
              <section className="space-y-2 border-t pt-4">
                <div className="flex items-center justify-between">
                  <p className="text-sm font-medium">{t("sectionLlm")}</p>
                  <StatusChip
                    set={!!settingsFor?.llm_config_present}
                    setLabel={t("statusSet")}
                    unsetLabel={t("statusUnset")}
                  />
                </div>
                {settingsFor?.llm_config_present ? (
                  <>
                    {/* #1399: destructive action stays outside the fold —
                      one click away, never behind a disclosure. */}
                    <Button
                      type="button"
                      variant="destructive-outline"
                      onClick={() => setLlmDeleteConfirm(true)}
                      disabled={settingsSaving || llmDeleting}
                    >
                      {llmDeleting && <InlineSpinner aria-hidden="true" />}
                      {t("llmDelete")}
                    </Button>
                    {/* #1399: a stored config folds the write-only inputs away
                      (empty fields under a 設定済 chip read as contradictory).
                      Keyed by connector so the uncontrolled open state never
                      leaks between connectors' dialogs (#1388 pattern). */}
                    <details
                      key={`llm-replace-${settingsFor.connector_id}`}
                      className="rounded-md border p-3"
                    >
                      <summary className="cursor-pointer text-sm font-medium">
                        {t("llmReplaceToggle")}
                      </summary>
                      <div className="mt-3 space-y-2">{llmFieldsBlock}</div>
                    </details>
                  </>
                ) : (
                  llmFieldsBlock
                )}
                {/* #1388: LiteLLM virtual key demoted to an advanced fold —
                  stored but not vended to the worker yet. Keyed by
                  connector so the uncontrolled <details> open state never
                  leaks from one connector's dialog into another's. */}
                <details
                  key={settingsFor?.connector_id ?? "none"}
                  className="rounded-md border p-3"
                >
                  <summary className="cursor-pointer text-sm font-medium">
                    {t("advancedSettings")}
                  </summary>
                  <div className="mt-3">
                    <label
                      htmlFor="conn-settings-litellm"
                      className="mb-1 block text-sm font-medium"
                    >
                      {t("litellmKeyLabel")}
                    </label>
                    <Input
                      id="conn-settings-litellm"
                      aria-label={t("litellmKeyLabel")}
                      value={litellmKey}
                      onChange={(e) => setLitellmKey(e.target.value)}
                    />
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t("litellmKeyNote")}
                    </p>
                  </div>
                  {/* #1471: memory_link_template was persistable via the API and
                    readable in the frontend type, but had no input — so the
                    feature could only be turned on by hand. Empty clears to
                    null; the server validates the scheme and template syntax. */}
                  <div className="mt-3">
                    <label
                      htmlFor="conn-settings-memory-link"
                      className="mb-1 block text-sm font-medium"
                    >
                      {t("memoryLinkTemplateLabel")}
                    </label>
                    <Input
                      id="conn-settings-memory-link"
                      aria-label={t("memoryLinkTemplateLabel")}
                      value={memoryLinkTemplate}
                      onChange={(e) => setMemoryLinkTemplate(e.target.value)}
                      placeholder={t("memoryLinkTemplatePlaceholder")}
                    />
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t("memoryLinkTemplateNote")}
                    </p>
                  </div>
                </details>
              </section>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setSettingsFor(null)}
              disabled={settingsSaving || llmDeleting}
            >
              {tCommon("cancel")}
            </Button>
            <Button
              onClick={() => void handleSettingsSave()}
              disabled={settingsSaving || llmDeleting}
            >
              {settingsSaving && <InlineSpinner aria-hidden="true" />}
              {t("settingsSave")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* #1388: destructive confirm for deleting the stored LLM bundle.
          Errors surface in the settings dialog's Alert (the PATCH runs
          while that dialog stays open). */}
      <AlertDialog
        open={llmDeleteConfirm}
        onOpenChange={(o) => !o && setLlmDeleteConfirm(false)}
      >
        <AlertDialogContent
          // Confirmed delete: the trigger (delete button) will unmount when
          // the in-flight PATCH resolves, so Radix's focus-return would drop
          // to document.body while the settings dialog is still open. Park
          // focus on the dialog container instead. llmDeleting is already
          // true here (set synchronously in the action's onClick); a
          // cancelled confirm leaves it false and keeps default focus-return.
          onCloseAutoFocus={(e) => {
            if (llmDeleting) {
              e.preventDefault();
              settingsContentRef.current?.focus();
            }
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>{t("llmDeleteConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("llmDeleteConfirmDesc")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={() => void handleLlmDelete()}>
              {tCommon("delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Create dialog (after Slack OAuth). A real Dialog (not AlertDialog):
          the submit is a form action that must keep the dialog open on
          failure so the error Alert is seen — an AlertDialogAction auto-closes
          on click and swallowed the create error (the #1409 "silent" report). */}
      <Dialog
        open={pending !== null}
        // Block escape/overlay close mid-submit so an in-flight failure isn't
        // swallowed with the dialog (mirrors the settings dialog).
        onOpenChange={(o) => {
          if (!o && !submitting) closeCreateDialog();
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("createTitle")}</DialogTitle>
            <DialogDescription>
              {t("createDesc", {
                team: pending?.team_name || pending?.team_id || "",
              })}
            </DialogDescription>
          </DialogHeader>
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
            {/* #1409: write-target selection. When the workspace already has
                contexts, offer connect-to-existing (default) vs create-new;
                with none, keep the original single create-name field so the
                first-connector flow is unchanged and fully backward compatible. */}
            <div>
              <p className="mb-1 block text-sm font-medium">
                {t("contextTargetLabel")}
              </p>
              {availableContexts.length > 0 ? (
                <>
                  <div
                    role="group"
                    aria-label={t("contextTargetLabel")}
                    className="mb-2 inline-flex rounded-lg bg-muted p-1"
                  >
                    <Button
                      type="button"
                      size="sm"
                      variant={contextMode === "existing" ? "default" : "ghost"}
                      aria-pressed={contextMode === "existing"}
                      onClick={() => setContextMode("existing")}
                    >
                      {t("contextModeExisting")}
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant={contextMode === "new" ? "default" : "ghost"}
                      aria-pressed={contextMode === "new"}
                      onClick={() => setContextMode("new")}
                    >
                      {t("contextModeNew")}
                    </Button>
                  </div>
                  {contextMode === "existing" ? (
                    <div>
                      <Select
                        value={selectedContextId}
                        onValueChange={setSelectedContextId}
                      >
                        <SelectTrigger
                          id="conn-existing-context"
                          aria-label={t("contextExistingLabel")}
                        >
                          <SelectValue
                            placeholder={t("contextExistingPlaceholder")}
                          />
                        </SelectTrigger>
                        <SelectContent>
                          {availableContexts.map((ctx) => (
                            <SelectItem key={ctx.id} value={ctx.id}>
                              {ctx.display_name || ctx.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {t("contextExistingHelp")}
                      </p>
                    </div>
                  ) : (
                    contextNameField
                  )}
                </>
              ) : (
                contextNameField
              )}
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
          <DialogFooter>
            <Button
              variant="outline"
              onClick={closeCreateDialog}
              disabled={submitting}
            >
              {tCommon("cancel")}
            </Button>
            <Button
              onClick={() => void handleCreate()}
              disabled={
                submitting ||
                !createBindingReady ||
                (piiEnabled && piiDetectors.length === 0)
              }
            >
              {submitting && <InlineSpinner aria-hidden="true" />}
              {t("createConnector")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
            {/* #1426: the worker fetches credentials itself, but the user still
                has two Slack-side actions before anything is ingested — surface
                them as a short checklist so "created" doesn't read as "done". */}
            <div className="rounded-md border p-3">
              <p className="mb-2 font-medium">{t("nextStepsTitle")}</p>
              <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
                <li>{t("nextStepInviteBot")}</li>
                <li>{t("nextStepSelectChannels")}</li>
              </ol>
            </div>
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
