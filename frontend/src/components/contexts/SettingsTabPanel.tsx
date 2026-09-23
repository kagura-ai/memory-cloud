/**
 * SettingsTabPanel
 *
 * Self-contained panel for the Settings tab in the consolidated context detail page.
 * Contains basic info, AI config, privacy with sticky save bar.
 * Extracted from contexts/[id]/settings/page.tsx (#232).
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/utils/clipboard";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Save,
  AlertCircle,
  Loader2,
  Brain,
  Lock,
  Copy,
  Info,
  Moon,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/base";
import { getContext, updateContext } from "@/lib/api/contexts";
import { getWorkspaceUsageCurrent } from "@/lib/api/workspaces";
import type { SleepContextsUsage } from "@/lib/api/usage";
import type { Context } from "@/lib/types/context";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { CONTEXT_TEMPLATES, getTemplate } from "@/lib/templates/usage-guide";
import { useFeatureGates } from "@/hooks/useFeatureGate";
import { usePlanTierMatrix } from "@/hooks/usePlanFeatures";
import {
  FeatureGateNotice,
  featureGateToast,
  type FeatureGateScope,
} from "@/components/common/FeatureGateNotice";
import {
  gateFromFacts,
  isBlocked,
  isGateKey,
  type GateKey,
} from "@/lib/gates/featureGates";

// #1583: the plan features this form can refuse on. #1646: each with the
// scope of its gate copy, read by the card's notice AND by the toast a
// refused save shows, so the two say the same thing. Sharing and publishing
// gate only the transition INTO shared / public — a context that already is
// keeps working — hence "create"; Sleep Maintenance is the feature as a whole.
// A plan refusal naming any other feature keeps the server text.
const REFUSAL_SCOPE: Readonly<Partial<Record<GateKey, FeatureGateScope>>> = {
  shared_contexts: "create",
  public_contexts: "create",
  sleep_reports: "all",
};

const SHARING_NOTICE_ID = "context-sharing-upgrade-notice";
const SLEEP_GATE_HINT_ID = "context-sleep-gate-hint";

interface SettingsTabPanelProps {
  contextId: string;
  context: Context;
  onContextUpdated: (context: Context) => void;
}

export function SettingsTabPanel({
  contextId,
  context,
  onContextUpdated,
}: SettingsTabPanelProps) {
  const { user } = useAuth();
  const { currentWorkspace } = useWorkspace();
  const { toast } = useToast();
  const t = useTranslations("contextSettings");
  const tCommon = useTranslations("common");
  const tGate = useTranslations("gate");
  const locale = useLocale();
  // #1645: every gate this form reads, from one set of subscriptions.
  // #1551: making a context public is XL-only. A context that is already
  // public keeps serving (and can still be unpublished) on any tier — only
  // the transition INTO public is gated here.
  // #1560: gate = tier matrix `public_contexts` boolean; `pending` while it
  // resolves (the Make Public control is withheld, no upsell yet).
  // #1583: the same for private → shared (`shared_contexts`). A context that
  // is already shared stays editable (and can be made private) on any tier.
  // The sleep gate is below, with the sleep quota.
  const gates = useFeatureGates([
    "public_contexts",
    "shared_contexts",
    "sleep_reports",
  ]);
  // #1645: the same shared matrix (module cache — no extra fetch), so a save
  // refusal that names no tier gets the pre-check's tier and labels.
  const tiers = usePlanTierMatrix();

  // #1583: the stored privacy flags, normalised the way the form state is
  // seeded — an older response omits them, and diffing the raw `undefined`
  // against the seeded default made an untouched control look changed.
  const storedIsPrivate = context.is_private ?? true;
  const storedIsPublic = context.is_public ?? false;
  // Only the transition is gated: going back to a stored "shared" is free.
  const sharingLocked =
    storedIsPrivate && gates.shared_contexts.state !== "allowed";
  // #1646: the refusal the card's notice explains — including the tier-less
  // one, when no served tier has sharing. Pending locks the control but
  // explains nothing yet (the notice renders nothing for it).
  const sharingRefused = storedIsPrivate && isBlocked(gates.shared_contexts);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state — initialised from context prop to avoid isDirty flash
  const [displayName, setDisplayName] = useState(context.display_name || "");
  const [description, setDescription] = useState(context.description || "");
  const [summary, setSummary] = useState(context.summary || "");
  const [usageGuide, setUsageGuide] = useState(context.usage_guide || "");
  const [isPrivate, setIsPrivate] = useState(context.is_private ?? true);
  const [isPublic, setIsPublic] = useState(context.is_public ?? false);
  const [resourceId, setResourceId] = useState("");

  const [idCopied, setIdCopied] = useState(false);
  const [privacyDialogOpen, setPrivacyDialogOpen] = useState(false);
  const [pendingPrivacyChange, setPendingPrivacyChange] = useState<
    boolean | null
  >(null);
  const [sleepMode, setSleepMode] = useState<"full" | "edges_only" | "skip">(
    context.sleep_mode,
  );
  const [skipDialogOpen, setSkipDialogOpen] = useState(false);
  // Issue #560: tier quota for sleep-enabled contexts
  const [sleepQuota, setSleepQuota] = useState<SleepContextsUsage | null>(null);

  const applyContextData = useCallback((data: Context) => {
    setDisplayName(data.display_name || "");
    setDescription(data.description || "");
    setSummary(data.summary || "");
    setUsageGuide(data.usage_guide || "");
    setIsPrivate(data.is_private ?? true);
    setIsPublic(data.is_public ?? false);
    setSleepMode(data.sleep_mode);
    setResourceId("");
  }, []);

  // Sync form state when context prop changes
  useEffect(() => {
    applyContextData(context);
  }, [context, applyContextData]);

  const refreshContext = useCallback(async () => {
    try {
      const data = await getContext(contextId);
      applyContextData(data);
      onContextUpdated(data);
      // Issue #560 follow-up: refetch sleep_contexts quota after save so a
      // skip→non-skip toggle increments `used` in the displayed `X / Y`
      // immediately. Owner-only since non-owners can't trigger the change.
      if (currentWorkspace?.current_user_role === "owner") {
        try {
          const usage = await getWorkspaceUsageCurrent();
          setSleepQuota(usage.usage.sleep_contexts ?? null);
        } catch {
          // Best-effort — if the refetch fails the displayed quota is
          // slightly stale until next mount; backend remains authoritative.
        }
      }
    } catch {
      // Silent refresh
    }
  }, [
    contextId,
    applyContextData,
    onContextUpdated,
    currentWorkspace?.current_user_role,
  ]);

  const handleSave = async () => {
    if (isPublic && !context.is_public && !resourceId.trim()) {
      toast({
        title: t("resourceIdRequired"),
        description: t("resourceIdRequiredDesc"),
        variant: "destructive",
      });
      return;
    }

    try {
      setSaving(true);
      setError(null);

      let resource_id: string | undefined = undefined;
      if (isPublic && !context.is_public) {
        resource_id = resourceId.trim();
      }

      // #1193: send ONLY changed fields (all ContextUpdate fields are
      // optional). Re-submitting untouched fields made the whole save fail
      // validation when a legacy value exceeded the current cap (e.g. an
      // MCP-written summary), blocking unrelated edits like sleep_mode.
      const payload: Parameters<typeof updateContext>[1] = {};
      if (displayName.trim() !== (context.display_name ?? "")) {
        payload.display_name = displayName.trim();
      }
      if (description.trim() !== (context.description ?? "")) {
        payload.description = description.trim();
      }
      if (summary.trim() !== (context.summary ?? "")) {
        payload.summary = summary.trim();
      }
      if (usageGuide.trim() !== (context.usage_guide ?? "")) {
        payload.usage_guide = usageGuide.trim();
      }
      if (isPrivate !== storedIsPrivate) {
        payload.is_private = isPrivate;
      }
      if (isPublic !== storedIsPublic) {
        payload.is_public = isPublic;
      }
      if (resource_id !== undefined) {
        payload.resource_id = resource_id;
      }
      if (isOwner && sleepMode !== context.sleep_mode) {
        payload.sleep_mode = sleepMode;
      }

      if (Object.keys(payload).length === 0) {
        // Everything was reverted to the original values — a true no-op.
        // Skip the request AND the refresh, clear the sticky save bar, and
        // say "no changes" instead of a misleading "Saved" (PR #1194 review).
        setIsDirty(false);
        toast({
          title: t("noChangesTitle"),
          description: t("noChangesDesc"),
        });
        return;
      }

      await updateContext(context.id, payload);

      toast({
        title: t("savedTitle"),
        description: t("savedDesc"),
      });
      await refreshContext();
    } catch (err: unknown) {
      // #1193: surface WHICH field failed validation — "Request validation
      // failed" alone is undiagnosable from the toast.
      let description =
        err instanceof Error ? err.message : t("saveFailedDesc");
      if (err instanceof ApiError) {
        // #1583: a plan refusal on one of this form's gates names the
        // control, not the feature key; other features keep the server text.
        // #1644: the tier is the one the refusal names, resolved to this
        // deployment's label. #1645: a refusal that names none (a server
        // predating #1644) takes the matrix's tier, as the pre-check does.
        // #1646: told with the gate notice's own copy, so the toast reads
        // like the card. When neither names a tier the tier-less copy says
        // so — but only once the matrix has answered: before that "no plan
        // includes it" would be a guess, so the server text stays. The
        // server's `sleep_mode` is not a GateKey; it alone lifts through the
        // fallback key — any other unknown feature keeps the server text
        // rather than being retold as Sleep Maintenance. A toast carries no
        // CTA, so the raw upgrade answer is moot here.
        const facts = err.gate;
        const gate =
          facts?.state === "plan" &&
          (isGateKey(facts.feature) || facts.feature === "sleep_mode")
            ? gateFromFacts(facts, {
                fallbackKey: "sleep_reports",
                canUpgrade: false,
                locale,
                tiers,
              })
            : null;
        const scope = gate ? REFUSAL_SCOPE[gate.feature] : undefined;
        const gateToast =
          gate &&
          scope !== undefined &&
          (gate.planLabel !== undefined || tiers !== null)
            ? featureGateToast(gate, tGate, scope)
            : null;
        if (gateToast) {
          toast({ ...gateToast, duration: 6000 });
          return;
        }
        // 422s carry {loc,msg,type}[] here (aliased from details.errors at
        // the transport layer); the declared string type covers legacy shapes.
        const fieldErrors = err.details?.detail as unknown;
        if (Array.isArray(fieldErrors) && fieldErrors.length > 0) {
          const first = fieldErrors[0] as { loc?: unknown[]; msg?: string };
          const field = Array.isArray(first.loc)
            ? String(first.loc[first.loc.length - 1])
            : "";
          if (first.msg) {
            description = `${description} — ${field ? `${field}: ` : ""}${first.msg}`;
          }
        }
      }
      toast({
        title: t("saveFailedTitle"),
        description,
        variant: "destructive",
        duration: 6000,
      });
    } finally {
      setSaving(false);
    }
  };

  const handlePrivacyToggle = (newIsPrivate: boolean) => {
    if (!storedIsPrivate && newIsPrivate) {
      setPendingPrivacyChange(newIsPrivate);
      setPrivacyDialogOpen(true);
    } else {
      setIsPrivate(newIsPrivate);
      markDirty();
    }
  };

  const confirmPrivacyChange = () => {
    if (pendingPrivacyChange !== null) {
      setIsPrivate(pendingPrivacyChange);
      markDirty();
    }
    setPrivacyDialogOpen(false);
    setPendingPrivacyChange(null);
  };

  const handleSleepModeChange = (value: string) => {
    if (value === "skip") {
      setSkipDialogOpen(true);
    } else if (value === "full" || value === "edges_only") {
      setSleepMode(value);
      markDirty();
    }
  };

  const confirmSkipModeChange = () => {
    setSleepMode("skip");
    markDirty();
    setSkipDialogOpen(false);
  };

  const cancelSkipModeChange = () => {
    setSkipDialogOpen(false);
  };

  const isOwner = currentWorkspace?.current_user_role === "owner";

  const [isDirty, setIsDirty] = useState(false);
  const markDirty = useCallback(() => setIsDirty(true), []);

  // Issue #560: fetch the workspace's sleep-enabled-contexts quota so we can
  // disable full/edges_only options when adding one more would exceed the
  // effective limit. Only owners can change sleep_mode (the section is
  // already gated by isOwner downstream), so we only fetch when relevant.
  useEffect(() => {
    if (!isOwner) {
      return;
    }
    let cancelled = false;
    getWorkspaceUsageCurrent()
      .then((response) => {
        if (cancelled) return;
        setSleepQuota(response.usage.sleep_contexts ?? null);
      })
      .catch(() => {
        // Best-effort fetch — backend remains authoritative on quota check.
        // Leaving sleepQuota=null hides the inline meta but keeps the Select
        // fully enabled; the server will still reject over-quota saves.
      });
    return () => {
      cancelled = true;
    };
  }, [isOwner, contextId]);

  // Saving the same value the server already accepted does NOT increase the
  // count, so we only block when the *current* mode is "skip" and there is
  // no headroom left. Lateral non-skip → non-skip and reductions are always
  // allowed by the backend, so we don't disable options for those cases.
  // #1645: a tier with no Sleep Maintenance at all is the `sleep_reports`
  // PLAN gate (the matrix's `sleep_enabled_contexts_limit > 0` and this
  // payload's `limit > 0` are one predicate through the server's zero floor).
  // The tier claim below reads the gate; the options keep the payload's own
  // "no headroom" answer too, so a gate still pending on the matrix (or on a
  // matrix outage) never unlocks what the server's figure already closes.
  const sleepTierBlocked = gates.sleep_reports.state === "plan";
  const wouldExceedSleepQuota =
    sleepQuota !== null &&
    context.sleep_mode === "skip" &&
    (sleepTierBlocked || sleepQuota.used >= sleepQuota.limit);
  // #1646: the tier half is told by the gate's control hint (and the Select
  // points at it). Pending names nothing, so it has no hint.
  const sleepGateHint = wouldExceedSleepQuota && sleepTierBlocked;
  // Only the cap-reached half is a quota (its copy is untouched, P-27).
  const sleepCapReached =
    sleepQuota !== null &&
    sleepQuota.limit > 0 &&
    sleepQuota.used >= sleepQuota.limit;

  // Reset dirty flag when context prop changes (after save/discard)
  useEffect(() => {
    setIsDirty(false);
  }, [context]);

  return (
    <>
      <div className="space-y-6">
        {error && (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {/* Basic Information */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Info className="h-5 w-5" />
              {t("basicInfoTitle")}
            </CardTitle>
            <CardDescription>{t("basicInfoDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>{t("contextId")}</Label>
              <div className="flex items-center gap-2">
                <Input
                  value={context.id}
                  disabled
                  className="font-mono text-xs"
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  onClick={async () => {
                    try {
                      // copyText degrades to an execCommand fallback before
                      // throwing (issue #987).
                      await copyText(context.id);
                      setIdCopied(true);
                      setTimeout(() => setIdCopied(false), 1500);
                    } catch {
                      // copyText already exhausted its execCommand fallback
                      // (#987); surface the shared actionable hint (the ID
                      // stays visible for manual copy) instead of a bare
                      // "copy failed" title, matching the other copy sites.
                      toast({
                        title: tCommon("error"),
                        description: tCommon("copyFailedManualHint"),
                        variant: "destructive",
                      });
                    }
                  }}
                >
                  {idCopied ? (
                    <span className="text-xs text-green-600 dark:text-green-400">
                      {t("copied")}
                    </span>
                  ) : (
                    <Copy className="h-4 w-4" />
                  )}
                </Button>
              </div>
              <p className="text-sm text-muted-foreground">
                {t("contextIdHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("contextName")}</Label>
              <Input value={context.name} disabled className="font-mono" />
              <p className="text-sm text-muted-foreground">
                {t("contextNameHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("displayName")}</Label>
              <Input
                value={displayName}
                onChange={(e) => {
                  setDisplayName(e.target.value);
                  markDirty();
                }}
                placeholder={context.name}
                maxLength={200}
              />
              <p className="text-sm text-muted-foreground">
                {t("displayNameHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("descriptionLabel")}</Label>
              <Textarea
                placeholder={t("descriptionPlaceholder")}
                value={description}
                onChange={(e) => {
                  setDescription(e.target.value);
                  markDirty();
                }}
                rows={2}
              />
            </div>
          </CardContent>
        </Card>

        {/* AI Configuration */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Brain className="h-5 w-5" />
              {t("aiConfigTitle")}
            </CardTitle>
            <CardDescription>{t("aiConfigDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>{t("templateLabel")}</Label>
              <Select
                onValueChange={(templateId) => {
                  const template = getTemplate(templateId);
                  if (template) {
                    setSummary(template.summary);
                    setUsageGuide(template.usage_guide);
                    markDirty();
                  }
                }}
              >
                <SelectTrigger>
                  <SelectValue placeholder={t("templatePlaceholder")} />
                </SelectTrigger>
                <SelectContent>
                  {CONTEXT_TEMPLATES.map((tpl) => (
                    <SelectItem key={tpl.id} value={tpl.id}>
                      <div className="flex items-center gap-2">
                        <span className="text-xs px-2 py-0.5 rounded bg-muted">
                          {tpl.category}
                        </span>
                        <span className="font-medium">{tpl.name}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                {t("templateHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("summaryLabel")}</Label>
              <Textarea
                placeholder={t("summaryPlaceholder")}
                value={summary}
                onChange={(e) => {
                  setSummary(e.target.value);
                  markDirty();
                }}
                // #1193: matches CONTEXT_SUMMARY_MAX_LENGTH (raised 500→2000
                // to legitimize MCP-written summaries).
                maxLength={2000}
                rows={3}
              />
              <p className="text-sm text-muted-foreground">
                {t("summaryHelp", { count: summary.length })}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("usageGuideLabel")}</Label>
              <Textarea
                placeholder={t("usageGuidePlaceholder")}
                value={usageGuide}
                onChange={(e) => {
                  setUsageGuide(e.target.value);
                  markDirty();
                }}
                maxLength={2000}
                rows={6}
              />
              <p className="text-sm text-muted-foreground">
                {t("usageGuideHelp", { count: usageGuide.length })}
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Privacy & Access Control */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Lock className="h-5 w-5" />
              {t("privacyTitle")}
            </CardTitle>
            <CardDescription>{t("privacyDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between p-4 border rounded-lg">
              <div className="flex-1">
                <p className="font-medium text-sm">
                  {isPrivate
                    ? `🔒 ${t("privateContext")}`
                    : `👥 ${t("sharedContext")}`}
                </p>
                <p className="text-sm text-muted-foreground">
                  {isPrivate ? t("privateContextDesc") : t("sharedContextDesc")}
                </p>
              </div>
              <Button
                variant={isPrivate ? "outline" : "default"}
                size="sm"
                // #1583: without `shared_contexts` a private context cannot
                // leave private, so the form can never bundle an edit with a
                // change the server is known to refuse. Pending (`null`)
                // waits too, without the upsell.
                disabled={sharingLocked}
                aria-describedby={
                  sharingRefused ? SHARING_NOTICE_ID : undefined
                }
                onClick={() => handlePrivacyToggle(!isPrivate)}
              >
                {isPrivate ? t("makeShared") : t("makePrivate")}
              </Button>
            </div>

            {sharingRefused && (
              <FeatureGateNotice
                gate={gates.shared_contexts}
                scope={REFUSAL_SCOPE.shared_contexts}
                id={SHARING_NOTICE_ID}
                className="mb-0"
              />
            )}

            {isOwner &&
              (isPrivate ? (
                // "Make it Shared first" would point at a disabled button.
                sharingRefused ? null : (
                  <Alert>
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>{t("makeSharedFirst")}</AlertDescription>
                  </Alert>
                )
              ) : isPublic || context.is_public ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between p-4 border rounded-lg">
                    <div className="flex-1">
                      <p className="font-medium text-sm">
                        🌍 {t("publicContext")}
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {t("publicContextDesc")}
                      </p>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="border-red-300 text-red-700 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-950"
                      onClick={() => {
                        setIsPublic(false);
                        markDirty();
                      }}
                    >
                      {t("unpublish")}
                    </Button>
                  </div>

                  {!isPublic && context.is_public && (
                    <Alert>
                      <AlertCircle className="h-4 w-4" />
                      <AlertDescription>
                        {t("unpublishPending")}
                      </AlertDescription>
                    </Alert>
                  )}

                  {isPublic && context.resource_id && (
                    <Alert>
                      <AlertCircle className="h-4 w-4" />
                      <AlertTitle>{t("publicSearchApi")}</AlertTitle>
                      <AlertDescription className="space-y-2">
                        <div>
                          <p className="text-xs font-medium mb-1">
                            {t("resourceIdLabel")}:
                          </p>
                          <code className="block p-2 bg-muted rounded text-xs font-mono break-all">
                            {context.resource_id}
                          </code>
                        </div>
                        <div>
                          <p className="text-xs font-medium mb-1">
                            {t("resourceIdEndpoint")}
                          </p>
                          <code className="block p-2 bg-muted rounded text-xs font-mono break-all">
                            POST /api/v1/public/{contextId}/search
                          </code>
                        </div>
                        <div className="flex gap-3 mt-2 text-xs">
                          <a
                            href="/workspace/integrations/credentials?tab=resource-tokens"
                            className="text-primary underline hover:text-primary/80"
                          >
                            {t("manageResourceTokens")}
                          </a>
                          <a
                            href="https://github.com/kagura-ai/kagura-memory-python-sdk"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-primary underline hover:text-primary/80"
                          >
                            {t("pythonSdk")}
                          </a>
                        </div>
                      </AlertDescription>
                    </Alert>
                  )}
                </div>
              ) : gates.public_contexts.state !== "allowed" ? (
                // Pending renders nothing: neither the control nor an upsell.
                <FeatureGateNotice
                  gate={gates.public_contexts}
                  scope={REFUSAL_SCOPE.public_contexts}
                  className="mb-0"
                />
              ) : (
                <div className="space-y-3">
                  <div className="space-y-2">
                    <Label>
                      {t("resourceIdLabel")}{" "}
                      <span className="text-red-500">*</span>
                    </Label>
                    <Input
                      placeholder={t("resourceIdPlaceholder")}
                      value={resourceId}
                      onChange={(e) => {
                        const value = e.target.value
                          .toLowerCase()
                          .replace(/[^a-z0-9_]/g, "");
                        setResourceId(value);
                      }}
                      className="font-mono text-sm"
                    />
                    <p className="text-sm text-muted-foreground">
                      {t("resourceIdHelp")}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    className="w-full"
                    disabled={!resourceId.trim()}
                    onClick={() => {
                      if (resourceId.trim()) {
                        setIsPublic(true);
                        markDirty();
                      }
                    }}
                  >
                    🌍 {t("makePublic")}
                  </Button>
                </div>
              ))}
          </CardContent>
        </Card>

        {isOwner && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Moon className="h-5 w-5" />
                {t("sleepModeTitle")}
              </CardTitle>
              <CardDescription>{t("sleepModeDesc")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label>{t("sleepModeLabel")}</Label>
                <Select value={sleepMode} onValueChange={handleSleepModeChange}>
                  <SelectTrigger
                    aria-describedby={
                      sleepGateHint ? SLEEP_GATE_HINT_ID : undefined
                    }
                  >
                    <SelectValue placeholder={t("sleepModePlaceholder")} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="full" disabled={wouldExceedSleepQuota}>
                      {t("sleepModeFull")}
                    </SelectItem>
                    <SelectItem
                      value="edges_only"
                      disabled={wouldExceedSleepQuota}
                    >
                      {t("sleepModeEdgesOnly")}
                    </SelectItem>
                    <SelectItem value="skip">{t("sleepModeSkip")}</SelectItem>
                  </SelectContent>
                </Select>
                {sleepQuota !== null && (
                  <p className="text-xs text-muted-foreground">
                    {t("sleepQuotaUsage", {
                      used: sleepQuota.used,
                      limit: sleepQuota.limit,
                      addon: sleepQuota.addon_bonus,
                    })}
                  </p>
                )}
                {sleepGateHint ? (
                  <FeatureGateNotice
                    variant="control"
                    gate={gates.sleep_reports}
                    showBadge={false}
                    id={SLEEP_GATE_HINT_ID}
                  />
                ) : (
                  wouldExceedSleepQuota &&
                  sleepCapReached && (
                    <p className="text-xs text-destructive">
                      {t("sleepQuotaExceeded")}
                    </p>
                  )
                )}
                <p className="text-sm text-muted-foreground">
                  {
                    {
                      full: t("sleepModeFullDesc"),
                      edges_only: t("sleepModeEdgesOnlyDesc"),
                      skip: t("sleepModeSkipDesc"),
                    }[sleepMode]
                  }
                </p>
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      {/* Privacy Change Confirmation Dialog */}
      <AlertDialog open={privacyDialogOpen} onOpenChange={setPrivacyDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("makePrivateTitle")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm text-muted-foreground">
                <div>{t("makePrivateDesc")}</div>
                <div className="text-yellow-600 dark:text-yellow-400 font-medium">
                  {t("makePrivateWarning")}
                </div>
                <div>{t("makePrivateReaddNote")}</div>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              onClick={() => {
                setPrivacyDialogOpen(false);
                setPendingPrivacyChange(null);
              }}
            >
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmPrivacyChange}
              className="bg-yellow-600 hover:bg-yellow-700"
            >
              {t("makePrivate")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={skipDialogOpen} onOpenChange={setSkipDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("sleepModeSkipTitle")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm text-muted-foreground">
                <div>{t("sleepModeSkipDesc")}</div>
                <div className="text-yellow-600 dark:text-yellow-400 font-medium">
                  {t("sleepModeSkipWarning")}
                </div>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={cancelSkipModeChange}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmSkipModeChange}
              className="bg-yellow-600 hover:bg-yellow-700"
            >
              {t("sleepModeSkipConfirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Sticky Save Bar — only rendered when dirty */}
      {isDirty && (
        <div className="fixed bottom-0 left-0 right-0 z-50 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="container flex items-center justify-between py-3 px-4 max-w-4xl mx-auto">
            <p className="text-sm text-muted-foreground">
              {t("unsavedChanges")}
            </p>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={refreshContext}>
                {t("discard")}
              </Button>
              <Button size="sm" onClick={handleSave} disabled={saving}>
                {saving ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Save className="h-4 w-4 mr-2" />
                )}
                {saving ? t("saving") : t("saveChanges")}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
