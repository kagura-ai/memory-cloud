"use client";

/**
 * Contexts Management Page
 *
 * Allows users to create, view, switch, and delete contexts.
 * Each context corresponds to a separate Qdrant collection for memory isolation.
 *
 * Issue #82: Context-based Multi-Collection Support
 * Issue #223: i18n support
 */

import { useEffect, useState, useCallback, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useMemoryContext } from "@/contexts/MemoryContextContext";
import { SleepModeBadge } from "@/components/contexts/SleepModeBadge";
import { CurrentContextBadge } from "@/components/contexts/CurrentContextBadge";
import { formatDateTime, formatRelativeTime } from "@/lib/utils/datetime";
import {
  Plus,
  FolderOpen,
  Brain,
  AlertCircle,
  AlertTriangle,
  Loader2,
  Zap,
  Settings2,
  ChevronDown,
  BarChart,
  MoreVertical,
  ShieldCheck,
  Lock,
  Users,
  Globe,
  Database,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  DialogTrigger,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { cn } from "@/lib/utils";
import { typography, colors } from "@/styles/design-tokens";
import {
  getContexts,
  createContext,
  getContextStats,
  getContextSearchConfig,
  updateContextSearchConfig,
  getEmbeddingModels,
  type EmbeddingModel,
} from "@/lib/api/contexts";
import { checkOpenAIKeyStatus } from "@/lib/api/workspaces";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { ApiError } from "@/lib/api/base";
import type { Context, ContextStats } from "@/lib/types/context";
import { CONTEXT_TEMPLATES, getTemplate } from "@/lib/templates/usage-guide";
import { createExternalAPIKey } from "@/lib/api/external-keys";
import { useToast } from "@/hooks/use-toast";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// Constants (must match backend validation)
const CONTEXT_NAME_PATTERN = /^[a-z0-9_-]+$/;

export default function ContextsPage() {
  const t = useTranslations("contexts");
  const tCommon = useTranslations("common");
  const tDetail = useTranslations("contextDetail");
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user } = useAuth();
  const locale = useLocale();
  const { currentWorkspace } = useWorkspace();
  // Current context is derived from URL via MemoryContextContext; the
  // server-side current_context_id was removed when contexts became
  // URL-driven, so this hook is the only source of truth.
  const { contextId: currentContextId } = useMemoryContext();
  const [contexts, setContexts] = useState<Context[]>([]);
  const [loading, setLoading] = useState(true);

  // Check if context quota is reached (Issue #188, corrected in #1487).
  //
  // This used to be `plan_name === "free" && contexts.length >= 1`, which
  // re-implemented the server's quota in the browser and got it wrong four
  // ways: it ignored the PLAN_*_MAX_CONTEXTS settings overrides, ignored the
  // purchasable addon bonus, never gated basic (3) or pro (20) at all, and hid
  // the quota dialog behind a control it had already disabled.
  //
  // The server now sends the effective cap it actually enforces, so read it.
  // `undefined` (older API, or workspace still loading) means "do not block" —
  // the create call is still authoritative and returns a clear error.
  //
  // Count with max(list, workspace stat) rather than either alone, because the
  // two are wrong in opposite directions:
  //   - `contexts.length` is ACCESS-FILTERED — GET /contexts hides other users'
  //     private contexts, so an admin can see 1 of 20 and think there is room,
  //     while the server counts all 20 and rejects.
  //   - `context_count` matches the server's own query exactly (workspace_id +
  //     deleted_at IS NULL, the same as the create check) but comes from the
  //     workspace payload, so it can lag by one right after a create.
  // Taking the larger never under-counts, which is the direction that produces
  // a promise the server then breaks.
  const maxContexts = currentWorkspace?.max_contexts;
  const usedContexts = Math.max(
    contexts.length,
    currentWorkspace?.context_count ?? 0,
  );
  const isQuotaReached =
    maxContexts !== undefined && usedContexts >= maxContexts;
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [hasOpenAIKey, setHasOpenAIKey] = useState<boolean | null>(null); // Issue #165: API key check

  // Create dialog state (Advanced)
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newContextName, setNewContextName] = useState("");
  const [newContextDisplayName, setNewContextDisplayName] = useState("");
  const [newContextDescription, setNewContextDescription] = useState("");
  const [newContextSummary, setNewContextSummary] = useState("");
  const [newContextUsageGuide, setNewContextUsageGuide] = useState("");
  // Note: Embedding model is now fixed via EMBEDDING_MODEL env var (single collection mode)
  const [isPrivate, setIsPrivate] = useState(true); // Issue #165: Privacy control
  const [newEmbeddingModel, setNewEmbeddingModel] = useState<string>(""); // Issue #49: empty = default
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // Quick Create dialog state (Issue #169)
  const [quickCreateDialogOpen, setQuickCreateDialogOpen] = useState(false);
  const [quickCreateName, setQuickCreateName] = useState("");
  const [quickCreateError, setQuickCreateError] = useState<string | null>(null);
  const [quickCreating, setQuickCreating] = useState(false);

  // Embedding models (Issue #49)
  const [embeddingModels, setEmbeddingModels] = useState<EmbeddingModel[]>([]);
  const [defaultEmbeddingModel, setDefaultEmbeddingModel] =
    useState<string>("");

  // API Key setup dialog state
  const [apiKeyDialogOpen, setApiKeyDialogOpen] = useState(false);
  const [apiKeyValue, setApiKeyValue] = useState("");
  const [apiKeySaving, setApiKeySaving] = useState(false);
  const [apiKeyError, setApiKeyError] = useState<string | null>(null);
  const { toast } = useToast();

  // Quota limit dialog state
  const [quotaDialogOpen, setQuotaDialogOpen] = useState(false);

  // Stats state
  const [contextStats, setContextStats] = useState<
    Record<string, ContextStats>
  >({});
  const [loadingStats, setLoadingStats] = useState<Record<string, boolean>>({});

  // Issue #398: admin/owner-only controls (create button, per-row kebab,
  // settings/connections/graph navigation). hasWorkspaceRole returns false
  // while currentWorkspace hydrates — controls stay hidden until role is known.
  const canManageContexts = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Admin,
  );
  // The kebab "Analysis" entry mirrors the analyses tab gating in
  // [id]/page.tsx: owner role AND workspace allowlist membership.
  // Both must be true for the menu to appear (#497).
  const canStartAnalysis =
    hasWorkspaceRole(
      currentWorkspace?.current_user_role,
      WorkspaceRole.Owner,
    ) && currentWorkspace?.analyses_enabled === true;
  const tAnalyses = useTranslations("analyses");

  const fetchContexts = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await getContexts();
      setContexts(response.contexts);
    } catch (err) {
      setError("Failed to load contexts");
    } finally {
      setLoading(false);
    }
  }, []);

  // Issue #1167: with BYOK off the key-status probe 404s and env keys serve
  // embeddings — skip the check so hasOpenAIKey stays null (creation enabled,
  // no "configure key" CTA pointing at a disabled page).
  const systemFeatures = useSystemFeatures();
  const byokEnabled = systemFeatures?.byok === true;
  const checkApiKey = useCallback(async () => {
    if (!user?.current_workspace_id) return;
    if (!byokEnabled) return;

    try {
      const status = await checkOpenAIKeyStatus(user.current_workspace_id);
      // #1495: gate on whether embedding WORKS, not on whether this workspace
      // owns a key. #1167 already handled the BYOK-off case above; this closes
      // the other half — BYOK on AND a platform credential set, where has_key
      // is false for a workspace that embeds perfectly well. Gating creation on
      // it is #1487 all over again.
      //
      // `?? null` so a server predating the field leaves the gate open rather
      // than blocking on a question it never answered.
      setHasOpenAIKey(status.embedding_available ?? null);
    } catch (err) {
      setHasOpenAIKey(null);
    }
  }, [user?.current_workspace_id, byokEnabled]);

  useEffect(() => {
    fetchContexts();
    // Fetch available embedding models
    getEmbeddingModels()
      .then((resp) => {
        setEmbeddingModels(resp.models);
        setDefaultEmbeddingModel(resp.default_model);
      })
      .catch(() => {}); // Non-critical: selector won't show
  }, [fetchContexts]);

  // v0.42 review #38: the key-status probe legitimately re-runs once the byok
  // flag resolves (checkApiKey depends on byokEnabled). Kept in its OWN effect
  // so that late flag resolution does not re-fire fetchContexts /
  // getEmbeddingModels above — which double-fetched contexts + models and
  // re-flashed the loading skeleton on every first visit.
  useEffect(() => {
    checkApiKey();
  }, [checkApiKey]);

  // Redirect ?edit=<id> to settings page (Issue #96: edit modal removed)
  const editHandled = useRef(false);
  useEffect(() => {
    const editId = searchParams.get("edit");
    if (editId && !editHandled.current) {
      editHandled.current = true;
      router.replace(`/workspace/contexts/${editId}?tab=settings`);
    }
  }, [searchParams, router]);

  // Auto-select Shared for Admins when opening create dialogs
  useEffect(() => {
    if (
      (createDialogOpen || quickCreateDialogOpen) &&
      currentWorkspace?.current_user_role === "admin"
    ) {
      setIsPrivate(false); // Admins can only create shared contexts
    }
  }, [
    createDialogOpen,
    quickCreateDialogOpen,
    currentWorkspace?.current_user_role,
  ]);

  // Issue #169: Quick Create - minimal form, just name
  const handleQuickCreate = async () => {
    if (!quickCreateName.trim()) {
      setQuickCreateError(t("nameRequired"));
      return;
    }

    if (!CONTEXT_NAME_PATTERN.test(quickCreateName)) {
      setQuickCreateError(t("invalidName"));
      return;
    }

    try {
      setQuickCreating(true);
      setQuickCreateError(null);
      await createContext({
        name: quickCreateName.trim(),
        is_private: isPrivate, // Issue #182: Privacy control
      });
      setQuickCreateDialogOpen(false);
      setQuickCreateName("");
      setIsPrivate(true); // Reset to default
      fetchContexts();
    } catch (err: unknown) {
      const apiError = err instanceof ApiError ? err : null;

      let errorMessage =
        apiError?.details?.detail ||
        (err instanceof Error ? err.message : t("failedToCreate"));

      // Translate common error messages
      if (errorMessage.includes("Context limit reached")) {
        const planMatch = errorMessage.match(/Your (\w+) plan/i);
        const limitMatch = errorMessage.match(/allows (\d+) context/i);
        const plan = planMatch ? planMatch[1] : "Free";
        const limit = limitMatch ? limitMatch[1] : "1";
        errorMessage = t("contextLimitReached", { plan, limit });
      } else if (
        errorMessage.includes("already exists") ||
        errorMessage.includes("name taken")
      ) {
        errorMessage = t("nameTaken");
      } else if (
        errorMessage.includes("Invalid") &&
        errorMessage.includes("name")
      ) {
        errorMessage = t("invalidName");
      }

      setQuickCreateError(errorMessage);
    } finally {
      setQuickCreating(false);
    }
  };

  // Advanced Create - full form with all options
  const handleCreateContext = async () => {
    if (!newContextName.trim()) {
      setCreateError(t("nameRequired"));
      return;
    }

    // Validate context name format (must match backend)
    if (!CONTEXT_NAME_PATTERN.test(newContextName)) {
      setCreateError(t("invalidName"));
      return;
    }

    try {
      setCreating(true);
      setCreateError(null);
      await createContext({
        name: newContextName.trim(),
        display_name: newContextDisplayName.trim() || undefined,
        description: newContextDescription.trim() || undefined,
        summary: newContextSummary.trim() || undefined,
        usage_guide: newContextUsageGuide.trim() || undefined,
        embedding_model: newEmbeddingModel || undefined,
        is_private: isPrivate, // Issue #165
      });
      setCreateDialogOpen(false);
      setNewContextName("");
      setNewContextDisplayName("");
      setNewContextDescription("");
      setNewContextSummary("");
      setNewContextUsageGuide("");
      setIsPrivate(true);
      fetchContexts();
    } catch (err: unknown) {
      let errorMessage =
        err instanceof Error ? err.message : t("failedToCreate");

      // Translate common error messages (but keep resource_id duplicates as-is)
      if (errorMessage.includes("already used")) {
        // Resource ID duplicate error - show API message as-is (includes context name)
        setCreateError(errorMessage);
      } else if (errorMessage.includes("Context limit reached")) {
        const planMatch = errorMessage.match(/Your (\w+) plan/i);
        const limitMatch = errorMessage.match(/allows (\d+) context/i);
        const plan = planMatch ? planMatch[1] : "Free";
        const limit = limitMatch ? limitMatch[1] : "1";
        setCreateError(t("contextLimitReached", { plan, limit }));
      } else if (
        errorMessage.includes("already exists") ||
        errorMessage.includes("name taken")
      ) {
        setCreateError(t("nameTaken"));
      } else if (
        errorMessage.includes("Invalid") &&
        errorMessage.includes("name")
      ) {
        setCreateError(t("invalidName"));
      } else {
        setCreateError(errorMessage);
      }
    } finally {
      setCreating(false);
    }
  };

  const handleViewStats = async (context: Context) => {
    router.push(`/workspace/contexts/${context.id}`);
  };

  const handleLoadStats = async (context: Context) => {
    if (loadingStats[context.id]) return;

    try {
      setLoadingStats((prev) => ({ ...prev, [context.id]: true }));
      const stats = await getContextStats(context.id);
      setContextStats((prev) => ({ ...prev, [context.id]: stats }));
    } catch (err) {
    } finally {
      setLoadingStats((prev) => ({ ...prev, [context.id]: false }));
    }
  };

  const handleSaveApiKey = async () => {
    try {
      setApiKeySaving(true);
      setApiKeyError(null);

      // Validation
      if (!apiKeyValue.trim()) {
        setApiKeyError(t("apiKeyRequired"));
        return;
      }

      if (!apiKeyValue.startsWith("sk-")) {
        setApiKeyError(t("invalidApiKeyFormat"));
        return;
      }

      // Create OpenAI API key
      await createExternalAPIKey({
        key_name: "OPENAI_API_KEY",
        provider: "openai",
        value: apiKeyValue.trim(),
      });

      // Success
      toast({
        title: tCommon("success"),
        description: t("apiKeySaved"),
      });

      // Refresh OpenAI key status
      await checkApiKey();

      // Close dialog and reset
      setApiKeyDialogOpen(false);
      setApiKeyValue("");
      setApiKeyError(null);
    } catch (err: unknown) {
      const apiErr = err instanceof ApiError ? err : null;
      let errorMessage = t("failedToSaveApiKey");
      if (
        apiErr?.status === 409 ||
        apiErr?.details?.detail?.includes("already exists")
      ) {
        errorMessage = t("apiKeyAlreadyExists");
      } else if (apiErr?.details?.detail) {
        errorMessage = apiErr.details.detail;
      } else if (err instanceof Error) {
        errorMessage = err.message;
      }

      setApiKeyError(errorMessage);
      toast({
        title: tCommon("error"),
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setApiKeySaving(false);
    }
  };

  return (
    <PageContainer>
      <PageHeader
        title={t("title")}
        description={t("subtitle")}
        actions={
          <div className="flex items-center gap-2">
            {/* Issue #169: Dropdown for Quick Create vs Advanced Create */}
            {/* Issue #398: admin/owner-only — hidden for member/viewer. */}
            {canManageContexts && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    size="sm"
                    className={colors.button.primary}
                    // #1487: NOT disabled — on either condition.
                    //
                    // A missing BYOK key must not gate this: context creation
                    // has no server-side key precondition (the create path
                    // makes the row, the search config and the collection with
                    // no embedding call and no key lookup) and the probe was
                    // added as *guidance* (#181), not an entitlement gate.
                    //
                    // A reached quota must not gate it either. This is the
                    // dropdown TRIGGER, and the only way to reach the quota
                    // dialog is a menu item inside it — disabling the trigger
                    // is what made that dialog dead code in the first place.
                    // The items below already route to the dialog when the
                    // quota is reached, so creation stays blocked while the
                    // explanation stays reachable.
                  >
                    <Plus className="h-4 w-4 mr-2" />
                    {t("newContext")}
                    <ChevronDown className="h-4 w-4 ml-1" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-56">
                  <DropdownMenuItem
                    onClick={() => {
                      if (isQuotaReached) {
                        setQuotaDialogOpen(true);
                      } else {
                        setQuickCreateDialogOpen(true);
                      }
                    }}
                  >
                    <Zap className="h-4 w-4 mr-2 text-amber-500" />
                    <div>
                      <div className="font-medium">{t("quickCreate")}</div>
                      <div className="text-xs text-muted-foreground">
                        {t("quickCreateDesc")}
                      </div>
                    </div>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    onClick={() => {
                      if (isQuotaReached) {
                        setQuotaDialogOpen(true);
                      } else {
                        setCreateDialogOpen(true);
                      }
                    }}
                  >
                    <Settings2 className="h-4 w-4 mr-2 text-blue-500" />
                    <div>
                      <div className="font-medium">{t("advancedCreate")}</div>
                      <div className="text-xs text-muted-foreground">
                        {t("advancedCreateDesc")}
                      </div>
                    </div>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        }
      />

      {/* Quota Warning (Issue #188) - Below header.

          #1488 Phase 4: the GATE was widened in #1487 from "free plan and one
          context" to "any plan at the cap the server sent", but this banner
          kept asserting the old rule verbatim — so a Pro workspace at 20/20
          was told "Free plan allows 1 context. Upgrade to Basic or Pro", which
          is false three ways and is the same misleading-explanation failure
          #1487 was filed for. State the plan and the cap actually in force. */}
      {isQuotaReached && (
        <div className="mb-6 text-sm text-yellow-600 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-3">
          ⚠️{" "}
          {t("quotaReachedDetail", {
            plan: currentWorkspace?.plan_name ?? "current",
            limit: maxContexts ?? 0,
          })}{" "}
          <a
            href="/workspace/settings/plan"
            className="underline hover:text-yellow-700 dark:hover:text-yellow-300 font-medium"
          >
            {t("quotaReachedPlansLink")}
          </a>
        </div>
      )}

      {/* #1487: the missing-key notice used to live ONLY in the
          `contexts.length === 0` empty state, so a workspace that already had
          contexts saw a disabled button and no explanation whatsoever — which
          is exactly how this got reported as a plan/quota problem. Creation is
          no longer blocked on the key, but the guidance still has to be
          reachable, so surface it here for the non-empty case too. */}
      {hasOpenAIKey === false && contexts.length > 0 && (
        <Alert className="mb-6 bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800">
          <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-500" />
          <AlertTitle className="text-amber-900 dark:text-amber-100">
            {t("setupNeededOpenAI")}
          </AlertTitle>
          <AlertDescription className="text-amber-800 dark:text-amber-200">
            {t("openAIKeyRequired")}
            <div className="mt-3">
              <Button
                variant="outline"
                size="sm"
                className="border-amber-300 dark:border-amber-700 text-amber-900 dark:text-amber-100 hover:bg-amber-100 dark:hover:bg-amber-800"
                onClick={() => setApiKeyDialogOpen(true)}
              >
                {t("configureApiKey")} →
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {/* Advanced Create Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("createDialogTitle")}</DialogTitle>
            <DialogDescription>{t("createDialogDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("contextName")}
              </label>
              <Input
                placeholder={t("contextNamePlaceholder")}
                value={newContextName}
                onChange={(e) =>
                  setNewContextName(e.target.value.toLowerCase())
                }
                className="font-mono"
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {t("contextNameHelp")}
              </p>
            </div>
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("displayName")}{" "}
                <span className="text-gray-400">
                  {t("summaryUsageTemplateOptional")}
                </span>
              </label>
              <Input
                placeholder={t("displayNamePlaceholder")}
                value={newContextDisplayName}
                onChange={(e) => setNewContextDisplayName(e.target.value)}
                maxLength={200}
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {t("displayNameHelp")}
              </p>
            </div>
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("descriptionOptional")}
              </label>
              <Textarea
                placeholder={t("contextDescriptionPlaceholder")}
                value={newContextDescription}
                onChange={(e) => setNewContextDescription(e.target.value)}
                rows={3}
              />
            </div>

            {/* Summary & Usage Guide Template */}
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("summaryUsageTemplate")}{" "}
                <span className="text-gray-400">
                  {t("summaryUsageTemplateOptional")}
                </span>
              </label>
              <Select
                onValueChange={(templateId) => {
                  const template = getTemplate(templateId);
                  if (template) {
                    setNewContextSummary(template.summary);
                    setNewContextUsageGuide(template.usage_guide);
                  }
                }}
              >
                <SelectTrigger>
                  <SelectValue placeholder={t("templatePlaceholder")} />
                </SelectTrigger>
                <SelectContent>
                  {CONTEXT_TEMPLATES.map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      <div className="flex items-center gap-2">
                        <span className="text-xs px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800">
                          {t.category}
                        </span>
                        <div>
                          <span className="font-medium">{t.name}</span>
                          <span className="text-xs text-muted-foreground ml-2">
                            - {t.description}
                          </span>
                        </div>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className={cn(typography.caption, colors.text.muted)}>
                {t("customizeManually")}
              </p>
            </div>

            {/* Summary for AI */}
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("summaryForAI")}{" "}
                <span className="text-gray-400">
                  {t("summaryUsageTemplateOptional")}
                </span>
              </label>
              <Textarea
                placeholder={t("summaryPlaceholder")}
                value={newContextSummary}
                onChange={(e) => setNewContextSummary(e.target.value)}
                // #1193: matches CONTEXT_SUMMARY_MAX_LENGTH (500→2000).
                maxLength={2000}
                rows={2}
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {t("summaryHelp")} {newContextSummary.length}/2000
              </p>
            </div>

            {/* Usage Guide for AI */}
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("usageGuideForAI")}{" "}
                <span className="text-gray-400">
                  {t("summaryUsageTemplateOptional")}
                </span>
              </label>
              <Textarea
                placeholder={t("usageGuidePlaceholder")}
                value={newContextUsageGuide}
                onChange={(e) => setNewContextUsageGuide(e.target.value)}
                maxLength={2000}
                rows={4}
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {t("usageGuideHelp")} {newContextUsageGuide.length}/2000
              </p>
            </div>

            {/* Embedding Model - Issue #49 */}
            {embeddingModels.filter((m) => m.available).length > 1 && (
              <div className="space-y-2">
                <label className={cn(typography.bodySmall, "font-medium")}>
                  {t("embeddingModel")}
                </label>
                <select
                  value={newEmbeddingModel}
                  onChange={(e) => setNewEmbeddingModel(e.target.value)}
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm"
                >
                  <option value="">
                    {t("embeddingModelDefault", {
                      model: defaultEmbeddingModel,
                    })}
                  </option>
                  {embeddingModels
                    .filter((m) => m.available)
                    .map((m) => (
                      <option key={m.name} value={m.name}>
                        {m.name} ({m.dimensions}d, {m.provider})
                      </option>
                    ))}
                </select>
                <p className={cn(typography.caption, colors.text.muted)}>
                  {t("embeddingModelHelp", {
                    default:
                      "Immutable after creation. Determines vector dimensions and search quality.",
                  })}
                </p>
              </div>
            )}

            {/* Privacy Control - Issue #182 */}
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("privacy")}{" "}
                <span className="text-red-500">{t("required")}</span>
              </label>
              <div className="space-y-2">
                {/* Private Option */}
                <label
                  className={`flex items-start gap-3 p-3 border-2 rounded cursor-pointer ${
                    isPrivate
                      ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
                      : "border-gray-200 dark:border-gray-700"
                  } ${
                    currentWorkspace?.current_user_role === "admin"
                      ? "opacity-60"
                      : ""
                  }`}
                >
                  <input
                    type="radio"
                    value="private"
                    checked={isPrivate}
                    onChange={() => {
                      if (currentWorkspace?.current_user_role !== "admin") {
                        setIsPrivate(true);
                      }
                    }}
                    disabled={currentWorkspace?.current_user_role === "admin"}
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      🔒 {t("privateOption")}
                      {currentWorkspace?.current_user_role === "admin" && (
                        <Badge
                          variant="outline"
                          className="ml-1 text-xs bg-gray-100 text-gray-700"
                        >
                          {t("ownerOnly")}
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {currentWorkspace?.current_user_role === "admin"
                        ? t("onlyOwnersCanCreatePrivate")
                        : t("privateAvailableAllPlans")}
                    </div>
                  </div>
                </label>

                {/* Shared Option */}
                <label
                  className={`flex items-start gap-3 p-3 border-2 rounded ${
                    !isPrivate
                      ? "border-purple-500 bg-purple-50 dark:bg-purple-900/20"
                      : "border-gray-200 dark:border-gray-700"
                  } ${
                    currentWorkspace?.plan_name === "free" ||
                    currentWorkspace?.plan_name === "basic"
                      ? "opacity-60 cursor-not-allowed"
                      : "cursor-pointer"
                  }`}
                >
                  <input
                    type="radio"
                    value="shared"
                    checked={!isPrivate}
                    onChange={() => {
                      // Issue #270: Only Pro plan can create shared contexts
                      if (currentWorkspace?.plan_name === "pro") {
                        setIsPrivate(false);
                      }
                    }}
                    disabled={
                      currentWorkspace?.plan_name === "free" ||
                      currentWorkspace?.plan_name === "basic"
                    }
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      👥 {t("sharedOption")}
                      {(currentWorkspace?.plan_name === "free" ||
                        currentWorkspace?.plan_name === "basic") && (
                        <Badge
                          variant="outline"
                          className="ml-1 text-xs bg-purple-100 text-purple-700"
                        >
                          {t("proPlan")}
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {currentWorkspace?.plan_name === "pro"
                        ? t("teamMembersAccess")
                        : t("upgradeToPro")}
                    </div>
                  </div>
                </label>
                {(currentWorkspace?.plan_name === "free" ||
                  currentWorkspace?.plan_name === "basic") && (
                  <Button
                    type="button"
                    variant="link"
                    size="sm"
                    className="h-auto p-0 text-xs text-purple-700 dark:text-purple-300"
                    onClick={() => router.push("/workspace/settings/plan")}
                  >
                    {t("upgradeToProCta")}
                  </Button>
                )}
              </div>
            </div>

            {createError && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{createError}</AlertDescription>
              </Alert>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCreateDialogOpen(false)}
            >
              {tCommon("cancel")}
            </Button>
            <Button onClick={handleCreateContext} disabled={creating}>
              {creating && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {creating ? t("creating") : t("create")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {error && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Empty State - Unified Design */}
      {loading ? (
        <SpinnerLoading size="lg" message="Loading contexts..." />
      ) : contexts.length === 0 ? (
        <Alert
          className={cn(
            hasOpenAIKey === false
              ? "bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800"
              : "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800",
          )}
        >
          {hasOpenAIKey === false ? (
            <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-500" />
          ) : (
            <Brain className="h-4 w-4 text-blue-600 dark:text-blue-500" />
          )}
          <AlertTitle
            className={cn(
              hasOpenAIKey === false
                ? "text-amber-900 dark:text-amber-100"
                : "text-blue-900 dark:text-blue-100",
            )}
          >
            {hasOpenAIKey === false
              ? t("setupNeededOpenAI")
              : t("noContextsYet")}
          </AlertTitle>
          <AlertDescription
            className={cn(
              hasOpenAIKey === false
                ? "text-amber-800 dark:text-amber-200"
                : "text-blue-800 dark:text-blue-200",
            )}
          >
            {hasOpenAIKey === false ? (
              <>
                {t("openAIKeyRequired")}
                <div className="flex gap-2 mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    className="border-amber-300 dark:border-amber-700 text-amber-900 dark:text-amber-100 hover:bg-amber-100 dark:hover:bg-amber-800"
                    onClick={() => setApiKeyDialogOpen(true)}
                  >
                    {t("configureApiKey")} →
                  </Button>
                  {/* #1487: was hard-disabled on the missing key. Creation has
                      no server-side key precondition, so let it through — the
                      amber notice above already says what to configure.
                      It must still honour the SAME gates as the header control,
                      though: this branch sits outside `canManageContexts`, so
                      without them a viewer would get a create button the header
                      correctly hides, and a workspace at its cap would get one
                      the header correctly disables. */}
                  {canManageContexts && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="border-amber-300 dark:border-amber-700 text-amber-900 dark:text-amber-100 hover:bg-amber-100 dark:hover:bg-amber-800"
                      // Route to the quota dialog rather than going dead, for
                      // the same reason as the header trigger: a disabled
                      // control with no reachable explanation is the bug.
                      onClick={() =>
                        isQuotaReached
                          ? setQuotaDialogOpen(true)
                          : setQuickCreateDialogOpen(true)
                      }
                    >
                      <Plus className="h-4 w-4 mr-2" />
                      {t("create")}
                    </Button>
                  )}
                </div>
              </>
            ) : !currentWorkspace?.current_user_role ? null : !hasWorkspaceRole(
                currentWorkspace.current_user_role,
                WorkspaceRole.Admin,
              ) ? (
              <>{t("createFirstContextNonAdmin")}</>
            ) : (
              <>
                {t("createFirstContext")}
                <div className="mt-3">
                  <Button
                    size="sm"
                    className="bg-blue-600 hover:bg-blue-700 text-white"
                    // Same quota routing as the amber branch above and the
                    // header control. "No contexts visible" does not mean "no
                    // contexts exist": the cap counts the workspace's contexts,
                    // and a member can see zero of them while every slot is
                    // taken by other people's private ones. Without this the
                    // page renders the quota banner AND an enabled Create in
                    // the same view, and the create fails at the server.
                    onClick={() =>
                      isQuotaReached
                        ? setQuotaDialogOpen(true)
                        : setCreateDialogOpen(true)
                    }
                  >
                    <Plus className="h-4 w-4 mr-2" />
                    {t("create")}
                  </Button>
                </div>
              </>
            )}
          </AlertDescription>
        </Alert>
      ) : (
        <div className="overflow-x-auto border border-gray-200 dark:border-gray-700 rounded-lg">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
                <th className="px-4 py-3 text-left font-medium text-gray-600 dark:text-gray-300">
                  {t("contextName")}
                </th>
                <th className="px-4 py-3 text-right font-medium text-gray-600 dark:text-gray-300">
                  {t("memories")}
                </th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">
                  {t("lastActivity")}
                </th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">
                  {t("visibility")}
                </th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">
                  {t("sleepModeBadgeHeader")}
                </th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">
                  {tCommon("actions")}
                </th>
              </tr>
            </thead>
            <tbody>
              {contexts.map((context) => {
                const isCurrent = context.id === currentContextId;
                return (
                  <tr
                    key={context.id}
                    aria-current={isCurrent ? "true" : undefined}
                    className={cn(
                      "border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/30",
                      isCurrent &&
                        "bg-brand-green-50 dark:bg-brand-green-900/10",
                    )}
                  >
                    {/* Name + inline badges */}
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <Brain className="h-4 w-4 text-brand-green-600 flex-shrink-0" />
                        <div>
                          <div className="flex items-center gap-1.5">
                            <span className="font-medium text-gray-900 dark:text-gray-100">
                              {context.display_name || context.name}
                            </span>
                            {isCurrent && <CurrentContextBadge />}
                            {context.is_default && (
                              <Badge
                                variant="outline"
                                className="text-[10px] px-1 py-0 bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-700"
                              >
                                {t("default")}
                              </Badge>
                            )}
                            {context.is_locked && (
                              <span
                                title={t("locked")}
                                aria-label={t("locked")}
                              >
                                <ShieldCheck className="h-3 w-3 text-amber-500" />
                              </span>
                            )}
                          </div>
                          {context.description && (
                            <div
                              className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[300px]"
                              title={context.description}
                            >
                              {context.description}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>

                    {/* Memories count */}
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Database className="h-3 w-3 text-gray-400" />
                        <span className="text-sm tabular-nums text-gray-700 dark:text-gray-300">
                          {context.memory_count.toLocaleString()}
                        </span>
                      </div>
                    </td>

                    {/* Last Activity */}
                    <td className="px-4 py-3 text-center text-xs text-gray-500 dark:text-gray-400">
                      {context.last_activity_at ? (
                        <span
                          title={formatDateTime(
                            context.last_activity_at,
                            user?.timezone,
                            locale,
                          )}
                        >
                          {formatRelativeTime(context.last_activity_at, locale)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>

                    {/* Visibility */}
                    <td className="px-4 py-3 text-center">
                      {context.is_public ? (
                        <span className="inline-flex items-center gap-1 text-xs text-purple-600 dark:text-purple-400">
                          <Globe className="h-3 w-3" />
                          {t("publicLabel")}
                        </span>
                      ) : context.is_private ? (
                        <span className="inline-flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400">
                          <Lock className="h-3 w-3" />
                          {t("privateOption")}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-xs text-green-600 dark:text-green-400">
                          <Users className="h-3 w-3" />
                          {t("sharedOption")}
                        </span>
                      )}
                    </td>

                    <td className="px-4 py-3 text-center">
                      <SleepModeBadge mode={context.sleep_mode} />
                    </td>

                    {/* Actions */}
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-center gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 w-7 p-0"
                          onClick={() => handleViewStats(context)}
                          title={t("viewUsage")}
                        >
                          <BarChart className="h-3.5 w-3.5" />
                        </Button>
                        {/* Issue #398: kebab navigates to admin-only tabs;
                          hide entirely for member/viewer (overview reachable
                          via the BarChart button above). */}
                        {canManageContexts && (
                          <DropdownMenu>
                            <DropdownMenuTrigger asChild>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-7 w-7 p-0"
                              >
                                <MoreVertical className="h-3.5 w-3.5" />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem
                                onClick={() =>
                                  router.push(
                                    `/workspace/contexts/${context.id}?tab=memories`,
                                  )
                                }
                              >
                                <Settings2 className="h-4 w-4 mr-2" />
                                {tDetail("tabs.memories")}
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                onClick={() =>
                                  router.push(
                                    `/workspace/contexts/${context.id}?tab=connections`,
                                  )
                                }
                              >
                                <Settings2 className="h-4 w-4 mr-2" />
                                {t("connections")}
                              </DropdownMenuItem>
                              <DropdownMenuItem
                                onClick={() =>
                                  router.push(
                                    `/workspace/contexts/${context.id}?tab=graph`,
                                  )
                                }
                              >
                                <Settings2 className="h-4 w-4 mr-2" />
                                {t("graph")}
                              </DropdownMenuItem>
                              {/* Issue #497: owner-only entry — placed
                                before Settings so the kebab order matches
                                the tab order (analyses → settings). */}
                              {canStartAnalysis && (
                                <DropdownMenuItem
                                  onClick={() =>
                                    router.push(
                                      `/workspace/contexts/${context.id}?tab=analyses`,
                                    )
                                  }
                                >
                                  <BarChart className="h-4 w-4 mr-2" />
                                  {tAnalyses("actions.newAnalysis")}
                                </DropdownMenuItem>
                              )}
                              <DropdownMenuItem
                                onClick={() =>
                                  router.push(
                                    `/workspace/contexts/${context.id}?tab=settings`,
                                  )
                                }
                              >
                                <Settings2 className="h-4 w-4 mr-2" />
                                {tCommon("settings")}
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Issue #169: Quick Create Dialog */}
      <Dialog
        open={quickCreateDialogOpen}
        onOpenChange={setQuickCreateDialogOpen}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Zap className="h-5 w-5 text-amber-500" />
              {t("quickCreateContext")}
            </DialogTitle>
            <DialogDescription>{t("quickCreateContextDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("contextName")}
              </label>
              <Input
                placeholder={t("contextNamePlaceholder")}
                value={quickCreateName}
                onChange={(e) =>
                  setQuickCreateName(e.target.value.toLowerCase())
                }
                className="font-mono"
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !quickCreating) {
                    handleQuickCreate();
                  }
                }}
                autoFocus
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {t("contextNameHelp")}
              </p>
            </div>

            {/* Privacy Control - Quick Create - Issue #182 */}
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, "font-medium")}>
                {t("privacy")}
              </label>
              <div className="space-y-2">
                {/* Private Option */}
                <label
                  className={`flex items-start gap-3 p-3 border-2 rounded cursor-pointer ${
                    isPrivate
                      ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
                      : "border-gray-200 dark:border-gray-700"
                  } ${
                    currentWorkspace?.current_user_role === "admin"
                      ? "opacity-60"
                      : ""
                  }`}
                >
                  <input
                    type="radio"
                    value="private"
                    checked={isPrivate}
                    onChange={() => {
                      if (currentWorkspace?.current_user_role !== "admin") {
                        setIsPrivate(true);
                      }
                    }}
                    disabled={currentWorkspace?.current_user_role === "admin"}
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      🔒 {t("privateOption")}
                      {currentWorkspace?.current_user_role === "admin" && (
                        <Badge
                          variant="outline"
                          className="ml-1 text-xs bg-gray-100 text-gray-700"
                        >
                          {t("ownerOnly")}
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {currentWorkspace?.current_user_role === "admin"
                        ? t("adminsCanOnlyCreateShared")
                        : t("onlyYouCanAccess")}
                    </div>
                  </div>
                </label>

                {/* Shared Option */}
                <label
                  className={`flex items-start gap-3 p-3 border-2 rounded ${
                    !isPrivate
                      ? "border-purple-500 bg-purple-50 dark:bg-purple-900/20"
                      : "border-gray-200 dark:border-gray-700"
                  } ${
                    currentWorkspace?.plan_name === "free" ||
                    currentWorkspace?.plan_name === "basic"
                      ? "opacity-60 cursor-not-allowed"
                      : "cursor-pointer"
                  }`}
                >
                  <input
                    type="radio"
                    value="shared"
                    checked={!isPrivate}
                    onChange={() => {
                      // Issue #270: Only Pro plan can create shared contexts
                      if (currentWorkspace?.plan_name === "pro") {
                        setIsPrivate(false);
                      }
                    }}
                    disabled={
                      currentWorkspace?.plan_name === "free" ||
                      currentWorkspace?.plan_name === "basic"
                    }
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      👥 {t("sharedOption")}
                      {(currentWorkspace?.plan_name === "free" ||
                        currentWorkspace?.plan_name === "basic") && (
                        <Badge
                          variant="outline"
                          className="ml-1 text-xs bg-purple-100 text-purple-700"
                        >
                          {t("pro")}
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {currentWorkspace?.plan_name === "pro"
                        ? t("teamMembersCanAccessShort")
                        : t("requiresProPlan")}
                    </div>
                  </div>
                </label>
                {(currentWorkspace?.plan_name === "free" ||
                  currentWorkspace?.plan_name === "basic") && (
                  <Button
                    type="button"
                    variant="link"
                    size="sm"
                    className="h-auto p-0 text-xs text-purple-700 dark:text-purple-300"
                    onClick={() => router.push("/workspace/settings/plan")}
                  >
                    {t("upgradeToProCta")}
                  </Button>
                )}
              </div>
            </div>

            {quickCreateError && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{quickCreateError}</AlertDescription>
              </Alert>
            )}
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setQuickCreateDialogOpen(false);
                setQuickCreateName("");
                setQuickCreateError(null);
              }}
            >
              {tCommon("cancel")}
            </Button>
            <Button onClick={handleQuickCreate} disabled={quickCreating}>
              {quickCreating && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {quickCreating ? t("creating") : tCommon("create")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* OpenAI API Key Setup Dialog */}
      <Dialog open={apiKeyDialogOpen} onOpenChange={setApiKeyDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("configureOpenAIKey")}</DialogTitle>
            <DialogDescription>{t("openAIKeyDialogDesc")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            {/* Error Display */}
            {apiKeyError && (
              <div className="p-3 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg">
                <p className="text-sm text-red-700 dark:text-red-300">
                  {apiKeyError}
                </p>
              </div>
            )}

            <div className="space-y-2">
              <Label htmlFor="api-key">{t("openAIApiKey")}</Label>
              <Input
                id="api-key"
                type="password"
                value={apiKeyValue}
                onChange={(e) => {
                  setApiKeyValue(e.target.value);
                  setApiKeyError(null); // Clear error on input change
                }}
                placeholder="sk-..."
                className="font-mono"
                autoComplete="off"
              />
              <p className="text-xs text-muted-foreground">
                {t("openAIKeyHelp")}{" "}
                <a
                  href="https://platform.openai.com/api-keys"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-blue-600 hover:underline"
                >
                  {t("openAIPlatform")}
                </a>
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setApiKeyDialogOpen(false);
                setApiKeyValue("");
                setApiKeyError(null);
              }}
              disabled={apiKeySaving}
            >
              {tCommon("cancel")}
            </Button>
            <Button
              onClick={handleSaveApiKey}
              disabled={apiKeySaving || !apiKeyValue.trim()}
            >
              {apiKeySaving ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t("savingApiKey")}
                </>
              ) : (
                t("saveApiKey")
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Quota Limit Dialog */}
      <AlertDialog open={quotaDialogOpen} onOpenChange={setQuotaDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-amber-500" />
              {t("quotaDialogTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("quotaDialogDescription")}
            </AlertDialogDescription>
            <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-3 mt-3">
              <p className="text-sm text-blue-900 dark:text-blue-100 font-medium mb-1">
                {t("quotaDialogUpgradeHeading")}
              </p>
              <p className="text-sm text-blue-800 dark:text-blue-200">
                {t("quotaDialogUpgradeBody")}
              </p>
            </div>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => router.push("/workspace/settings/plan")}
            >
              {t("viewPlans")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
