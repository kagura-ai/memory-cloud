/**
 * SearchSettingsSection
 *
 * Self-contained section for search/reranker settings within the Settings tab.
 * Contains hybrid search weights, reranker config, content chunk vectors
 * (#2104), the recall LLM selector (#2148), embedding info with sticky save
 * bar.
 * Extracted from contexts/[id]/search-settings/page.tsx (#232).
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
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
  Settings,
  Save,
  AlertCircle,
  Info,
  Database,
  Loader2,
  Sparkles,
  Layers,
  ListFilter,
} from "lucide-react";
import {
  getContextSearchConfig,
  updateContextSearchConfig,
  type ContentChunksDisabledReason,
  type ContextSearchConfig,
  type ContextSearchConfigUpdate,
  type LlmSelectorDisabledReason,
} from "@/lib/api/contexts";
import {
  listExternalAPIKeys,
  type ExternalAPIKey,
} from "@/lib/api/external-keys";
import { apiClient, ApiError } from "@/lib/api/base";
import { useToast } from "@/hooks/use-toast";
import { useSystemFeatures, useSystemInfo } from "@/hooks/useSystemFeatures";
import { useFeatureGate } from "@/hooks/useFeatureGate";
import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { cn } from "@/styles/design-tokens";

interface TelemetryServiceStatus {
  status: string;
  details?: Record<string, unknown>;
}

interface TelemetryResponse {
  services: Record<string, TelemetryServiceStatus>;
}

const VOYAGE_MODELS = [
  { value: "rerank-2", label: "rerank-2 (Best quality)" },
  { value: "rerank-2-lite", label: "rerank-2-lite (Faster, cheaper)" },
];

const COHERE_MODELS = [
  { value: "rerank-multilingual-v3.0", label: "Multilingual v3.0" },
  { value: "rerank-english-v3.0", label: "English v3.0" },
];

const SELF_HOSTED_MODELS = [
  {
    value: "dengcao/Qwen3-Reranker-8B:Q5_K_M",
    label: "Qwen3-Reranker-8B (Best quality)",
  },
  {
    value: "bge-reranker-v2-m3",
    label: "BGE Reranker v2 M3 (Multilingual)",
  },
];

const DEFAULT_RERANKER_MODELS: Record<string, string> = {
  voyage: "rerank-2",
  cohere: "rerank-multilingual-v3.0",
  self_hosted: "dengcao/Qwen3-Reranker-8B:Q5_K_M",
};

interface SearchSettingsSectionProps {
  contextId: string;
}

export function SearchSettingsSection({
  contextId,
}: SearchSettingsSectionProps) {
  const t = useTranslations("searchSettings");
  const tCommon = useTranslations("common");

  const [config, setConfig] = useState<ContextSearchConfig | null>(null);
  const [editedConfig, setEditedConfig] = useState<
    Partial<ContextSearchConfigUpdate>
  >({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [externalKeys, setExternalKeys] = useState<ExternalAPIKey[]>([]);
  // Whether the external-keys list actually loaded. GET /external-keys is
  // workspace-owner-only, so it 403s for a non-owner editing this config even
  // when BYOK is on — in which case we must NOT assert a provider is
  // unavailable (Copilot review). Only a successful load makes key presence
  // "known".
  const [externalKeysLoaded, setExternalKeysLoaded] = useState(false);
  const [selfHostedAvailable, setSelfHostedAvailable] = useState(false);
  // #2104: the content chunk confirmation. The target is kept apart from
  // `open` so the dialog keeps its wording through the close animation.
  const [chunkDialogOpen, setChunkDialogOpen] = useState(false);
  const [chunkTarget, setChunkTarget] = useState(false);
  // #2148: the LLM selector confirmation (asked on enable only — turning it
  // off sends nothing anywhere and deletes nothing).
  const [selectorDialogOpen, setSelectorDialogOpen] = useState(false);
  const { toast } = useToast();
  // Issue #1167: with BYOK off no new key can be added, so the "configure
  // reranker keys" CTA would point at a page without an Add button — skip the
  // key probe and the CTA. (The list route itself still answers for the owner;
  // this component simply has no use for the answer then.)
  const systemFeatures = useSystemFeatures();
  const byokEnabled = systemFeatures?.byok === true;
  // #1572: the deployment default new contexts get, and whether the deployment
  // reranks at all. Both `null`/undefined while /system/info is in flight (and
  // `search_defaults` is absent on older backends) — render the plain copy then.
  const systemInfo = useSystemInfo();
  const searchDefaults = systemInfo?.search_defaults ?? null;
  // #1645: one gate for the reranker — the tier matrix's `reranking` and the
  // deployment's `reranking` flag. The flag keeps #1580's polarity (in
  // GATE_SPECS, `whenAbsent: true`): only an explicit false hides the card —
  // loading and older backends (no flag) keep it. Hidden, not greyed out: a
  // deployment that does not offer reranking has nothing for a workspace
  // owner to configure. `pending` keeps the controls inert and the upsell
  // unsaid until both the matrix and /system/info have answered.
  const rerank = useFeatureGate("reranking");
  const rerankingDisabledByDeployment = rerank.state === "deployment";
  const isDirty = Object.keys(editedConfig).length > 0;

  const providerLabel = (provider: string) =>
    provider === "voyage"
      ? "Voyage AI"
      : provider === "cohere"
        ? "Cohere"
        : t("selfHostedLocal");

  const loadExternalKeys = useCallback(async () => {
    try {
      const keys = await listExternalAPIKeys();
      setExternalKeys(keys.filter((k) => k.enabled));
      setExternalKeysLoaded(true);
    } catch {
      // 403 (non-owner) / network — key presence stays unknown.
      setExternalKeys([]);
      setExternalKeysLoaded(false);
    }
  }, []);

  const loadTelemetry = useCallback(async () => {
    try {
      const telemetry = await apiClient.get<TelemetryResponse>(
        "/api/v1/system/telemetry",
      );
      setSelfHostedAvailable(telemetry.services?.self_hosted?.status === "ok");
    } catch {
      setSelfHostedAvailable(false);
    }
  }, []);

  const loadConfig = useCallback(async () => {
    if (!contextId) return;

    try {
      setLoading(true);
      setError(null);
      const data = await getContextSearchConfig(contextId);
      setConfig(data);
      setEditedConfig({});
    } catch (err: unknown) {
      const errorMsg = err instanceof Error ? err.message : t("errorLoad");
      setError(errorMsg);
      toast({
        title: tCommon("error"),
        description: errorMsg,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  }, [contextId, t, tCommon, toast]);

  const refreshConfig = useCallback(async () => {
    if (!contextId) return;
    try {
      const data = await getContextSearchConfig(contextId);
      setConfig(data);
      setEditedConfig({});
    } catch {
      // Silent refresh
    }
  }, [contextId]);

  useEffect(() => {
    Promise.all([loadConfig(), loadTelemetry()]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextId]);

  // Issue #1167: probe external keys only once the byok flag resolves enabled
  // (kept out of the effect above so a late flag resolution doesn't re-fire
  // the config/telemetry loads). #1580: the keys only feed the reranker card,
  // so skip the probe when the deployment does not rerank.
  useEffect(() => {
    if (byokEnabled && !rerankingDisabledByDeployment) loadExternalKeys();
  }, [byokEnabled, rerankingDisabledByDeployment, loadExternalKeys]);

  const handleSave = useCallback(async () => {
    if (!contextId || Object.keys(editedConfig).length === 0) return;

    // #2104: the content chunk flag goes out only when the user changed it —
    // the server treats it as optional, and turning it on is plan/cap-checked.
    const chunksChanged =
      editedConfig.content_chunks_enabled !== undefined &&
      editedConfig.content_chunks_enabled !== config!.content_chunks_enabled;
    // #2148: the same for the LLM selector flag (plan/cap-checked on enable).
    const selectorChanged =
      editedConfig.llm_selector_enabled !== undefined &&
      editedConfig.llm_selector_enabled !== config!.llm_selector_enabled;

    try {
      setSaving(true);
      setError(null);

      const updateData: ContextSearchConfigUpdate = {
        semantic_weight:
          editedConfig.semantic_weight ?? config!.semantic_weight,
        bm25_weight: editedConfig.bm25_weight ?? config!.bm25_weight,
        fetch_factor: editedConfig.fetch_factor ?? config!.fetch_factor,
        use_rerank: editedConfig.use_rerank ?? config!.use_rerank,
        reranker_provider:
          editedConfig.reranker_provider ?? config!.reranker_provider,
        reranker_model: editedConfig.reranker_model ?? config!.reranker_model,
        ...(chunksChanged
          ? { content_chunks_enabled: editedConfig.content_chunks_enabled }
          : {}),
        ...(selectorChanged
          ? { llm_selector_enabled: editedConfig.llm_selector_enabled }
          : {}),
      };

      await updateContextSearchConfig(contextId, updateData);
      toast({ title: tCommon("success"), description: t("configSaved") });
      // Also re-reads content_chunks_backfill_pending for the progress note.
      await refreshConfig();
    } catch (err: unknown) {
      const errorMsg = err instanceof Error ? err.message : t("errorSave");
      setError(errorMsg);
      toast({
        title: tCommon("error"),
        description: errorMsg,
        variant: "destructive",
      });
      // #2104 / #2148: a refused chunk or LLM selector change (403 FEAT-001
      // plan / 429 QUOTA-001 cap, e.g. another context took the last slot
      // meanwhile): re-read the server's view so the card shows the reason,
      // and drop only the refused staged flag — the user's other unsaved
      // edits stay. Any other failure (network, 500, a capacity lock) keeps
      // the confirmed choice staged.
      const gate = err instanceof ApiError ? err.gate : undefined;
      const chunkRefusal =
        chunksChanged &&
        (gate?.feature === "content_chunks" ||
          gate?.quotaType === "content_chunk_contexts");
      const selectorRefusal =
        selectorChanged &&
        (gate?.feature === "llm_selector" ||
          gate?.quotaType === "llm_selector_contexts");
      if (chunkRefusal || selectorRefusal) {
        try {
          const data = await getContextSearchConfig(contextId);
          setConfig(data);
          setEditedConfig((prev) => {
            const next = { ...prev };
            if (chunkRefusal) delete next.content_chunks_enabled;
            if (selectorRefusal) delete next.llm_selector_enabled;
            return next;
          });
        } catch {
          // Silent: the toast above already reported the failure.
        }
      }
    } finally {
      setSaving(false);
    }
  }, [contextId, editedConfig, config, t, tCommon, toast, refreshConfig]);

  const handleWeightChange = (semantic: number) => {
    if (isNaN(semantic)) return;
    const clamped = Math.max(0, Math.min(1, semantic));
    setEditedConfig({
      ...editedConfig,
      semantic_weight: clamped,
      bm25_weight: parseFloat((1.0 - clamped).toFixed(2)),
    });
  };

  const handleProviderChange = (
    provider: "voyage" | "cohere" | "self_hosted",
  ) => {
    setEditedConfig({
      ...editedConfig,
      reranker_provider: provider,
      // #1572: picking the deployment's default provider preselects the model
      // the deployment actually serves (e.g. a vLLM served-model-name the
      // static list below cannot know).
      reranker_model:
        searchDefaults && provider === searchDefaults.reranker_provider
          ? searchDefaults.reranker_model
          : DEFAULT_RERANKER_MODELS[provider],
    });
  };

  const getCurrentValue = <K extends keyof ContextSearchConfig>(
    key: K,
  ): ContextSearchConfig[K] => {
    return (
      (editedConfig[key as keyof ContextSearchConfigUpdate] as
        ContextSearchConfig[K] | undefined) ??
      (config?.[key] as ContextSearchConfig[K])
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-brand-green-200 border-t-brand-green-600" />
      </div>
    );
  }

  if (!config) {
    return (
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>{error || t("failedToLoad")}</AlertDescription>
      </Alert>
    );
  }

  const hasVoyageKey = externalKeys.some(
    (k) => k.provider.toLowerCase() === "voyage" && k.enabled,
  );
  const hasCohereKey = externalKeys.some(
    (k) => k.provider.toLowerCase() === "cohere" && k.enabled,
  );
  // Issue #1167 / v0.42 review #0: we can only assert a voyage/cohere provider
  // is "unavailable" when the external-keys list actually loaded. It does NOT
  // load when BYOK is off (we skip the probe) OR for a non-owner editing this
  // config (GET /external-keys is owner-only → 403) — in both cases the backend may
  // still resolve a stored key, so treating the provider as unavailable would
  // wrongly disable Save (and the "turn rerank off" control). self_hosted
  // availability is independent (telemetry-derived).
  const externalKeysKnown = externalKeysLoaded;
  const hasAnyRerankerAvailable =
    !externalKeysKnown || hasVoyageKey || hasCohereKey || selfHostedAvailable;
  // #1572: no "configure keys" CTA when BYOK is off (#1167) OR the deployment
  // default is the keyless self_hosted reranker and it is reachable — there is
  // nothing to configure in either case.
  const keylessDefault =
    searchDefaults?.reranker_provider === "self_hosted" && selfHostedAvailable;
  const showConfigureKeysCta = byokEnabled && !keylessDefault;
  const controlsDisabled =
    rerank.state !== "allowed" || !hasAnyRerankerAvailable;

  const currentProvider = getCurrentValue("reranker_provider");
  const currentModel = getCurrentValue("reranker_model");
  const availableModels = [
    ...(currentProvider === "voyage"
      ? VOYAGE_MODELS
      : currentProvider === "self_hosted"
        ? SELF_HOSTED_MODELS
        : COHERE_MODELS),
  ];
  if (currentModel && !availableModels.some((m) => m.value === currentModel)) {
    // A deployment-default (or hand-set) model the static list doesn't know —
    // keep it selectable instead of rendering an empty select (#1572).
    availableModels.push({ value: currentModel, label: currentModel });
  }

  const selectedProviderUnavailable =
    (externalKeysKnown && currentProvider === "voyage" && !hasVoyageKey) ||
    (externalKeysKnown && currentProvider === "cohere" && !hasCohereKey) ||
    (currentProvider === "self_hosted" && !selfHostedAvailable);

  const useRerank = getCurrentValue("use_rerank");
  // #1580: with the card hidden the provider can be neither seen nor fixed,
  // and the deployment never calls it — don't block saving the other settings.
  const cannotSave =
    !rerankingDisabledByDeployment &&
    isDirty &&
    useRerank &&
    selectedProviderUnavailable;

  // ── #2104: content chunk vectors ──
  // Every answer is the server's (GET search-config, #2091 / #2103): the
  // plan gate applies only where the deployment turns it on, so the tier
  // matrix is not consulted here. A backend predating the fields omits
  // `content_chunks_enabled` and the card is not rendered.
  const chunksShown = config.content_chunks_enabled !== undefined;
  const storedChunks = config.content_chunks_enabled === true;
  const currentChunks = getCurrentValue("content_chunks_enabled") === true;
  const chunksCanChange = config.content_chunks_can_change === true;
  const chunksReason = config.content_chunks_disabled_reason ?? null;
  const chunksInUse = config.content_chunks_in_use ?? 0;
  const chunksLimit = config.content_chunks_limit ?? 0;
  // Stored on but not acted on (plan or cap, #2103); the chunk vectors are
  // kept. A backend without the field is taken at the stored value.
  const chunksInactive =
    storedChunks && config.content_chunks_effective === false;
  const chunksInactiveForPlan =
    chunksInactive && (chunksReason === "plan" || chunksReason === "cap");
  // A backend predating #2103 computes can_change from the owner rule alone,
  // so a store without chunk vectors (LanceDB) must block turning it on here.
  const chunksUnsupportedOn =
    !storedChunks && config.content_chunks_supported === false;
  const chunksSwitchBlocked = !chunksCanChange || chunksUnsupportedOn;
  // The reason shown under the switch: why it is disabled, or why a stored
  // flag is inactive when the inactive note does not already say it (e.g.
  // unsupported). A reason the note explains leaves only the role to say; a
  // backend predating the reason field could only refuse on role or store.
  const fallbackReason: ContentChunksDisabledReason | null =
    config.content_chunks_supported === false ? "unsupported" : null;
  const chunksBlockedReason: ContentChunksDisabledReason | null =
    chunksSwitchBlocked
      ? chunksInactiveForPlan
        ? "permission"
        : (chunksReason ?? fallbackReason ?? "permission")
      : chunksInactive && !chunksInactiveForPlan
        ? (chunksReason ?? fallbackReason)
        : null;
  const chunksBackfillPending = config.content_chunks_backfill_pending ?? 0;

  // Literal keys on purpose: the message-orphan scan (parity.test.ts) reads
  // a computed key as a reference to the whole namespace.
  const chunkReasonText = (reason: ContentChunksDisabledReason) => {
    switch (reason) {
      case "plan":
        return t("contentChunks.reasonPlan");
      case "cap":
        return t("contentChunks.reasonCap", {
          inUse: chunksInUse,
          limit: chunksLimit,
        });
      case "unsupported":
        return t("contentChunks.reasonUnsupported");
      case "permission":
        return t("contentChunks.reasonPermission");
    }
  };

  const handleChunksToggle = (checked: boolean) => {
    if (checked === storedChunks) {
      // Back to the saved value: nothing to confirm, nothing to send.
      const next = { ...editedConfig };
      delete next.content_chunks_enabled;
      setEditedConfig(next);
      return;
    }
    setChunkTarget(checked);
    setChunkDialogOpen(true);
  };

  const confirmChunksToggle = () => {
    setEditedConfig({ ...editedConfig, content_chunks_enabled: chunkTarget });
    setChunkDialogOpen(false);
  };

  // ── #2148: the recall LLM selector ──
  // The content chunk card's shape: every answer is the server's (GET
  // search-config, #2144) — the plan gate applies only where the deployment
  // sets LLM_SELECTOR_REQUIRE_PLAN, so the tier matrix is not consulted. A
  // backend predating the fields omits `llm_selector_enabled` and the card is
  // not rendered.
  const selectorShown = config.llm_selector_enabled !== undefined;
  const storedSelector = config.llm_selector_enabled === true;
  const currentSelector = getCurrentValue("llm_selector_enabled") === true;
  const selectorCanChange = config.llm_selector_can_change === true;
  const selectorReason = config.llm_selector_disabled_reason ?? null;
  const selectorInUse = config.llm_selector_in_use ?? 0;
  // null: the deployment does not gate the selector by plan (no cap to show).
  const selectorLimit = config.llm_selector_limit ?? null;
  // Stored on but not acted on (plan or cap). A backend without the field is
  // taken at the stored value.
  const selectorInactive =
    storedSelector && config.llm_selector_effective === false;
  const selectorInactiveForPlan =
    selectorInactive && (selectorReason === "plan" || selectorReason === "cap");
  const selectorSwitchBlocked = !selectorCanChange;
  // The reason under a disabled switch. When the inactive note already says
  // plan/cap, the switch is blocked only by the role (turning it off is
  // otherwise always allowed).
  const selectorBlockedReason: LlmSelectorDisabledReason | null =
    selectorSwitchBlocked
      ? selectorInactiveForPlan
        ? "permission"
        : (selectorReason ?? "permission")
      : null;
  // In-use / limit where the deployment gates by plan and the plan has
  // slots; the cap reason and the inactive cap note say it themselves.
  const showSelectorUsage =
    selectorLimit !== null && selectorLimit > 0 && selectorReason !== "cap";

  // Literal keys on purpose (parity.test.ts orphan scan, see above).
  const selectorReasonText = (reason: LlmSelectorDisabledReason) => {
    switch (reason) {
      case "plan":
        return t("llmSelector.reasonPlan");
      case "cap":
        return t("llmSelector.reasonCap", {
          inUse: selectorInUse,
          limit: selectorLimit ?? 0,
        });
      case "permission":
        return t("llmSelector.reasonPermission");
    }
  };

  const handleSelectorToggle = (checked: boolean) => {
    if (checked === storedSelector) {
      // Back to the saved value: nothing to confirm, nothing to send.
      const next = { ...editedConfig };
      delete next.llm_selector_enabled;
      setEditedConfig(next);
      return;
    }
    if (checked) {
      setSelectorDialogOpen(true);
      return;
    }
    setEditedConfig({ ...editedConfig, llm_selector_enabled: false });
  };

  const confirmSelectorEnable = () => {
    setEditedConfig({ ...editedConfig, llm_selector_enabled: true });
    setSelectorDialogOpen(false);
  };

  const getProviderDescription = () => {
    if (selfHostedAvailable && hasVoyageKey && hasCohereKey)
      return t("allProvidersAvailable");
    if (selfHostedAvailable && hasVoyageKey) return t("selfHostedAndVoyage");
    if (selfHostedAvailable && hasCohereKey) return t("selfHostedAndCohere");
    if (selfHostedAvailable) return t("selfHostedOnly");
    if (hasVoyageKey && hasCohereKey) return t("bothAvailable");
    if (hasVoyageKey) return t("voyageConfigured");
    if (hasCohereKey) return t("cohereConfigured");
    return "";
  };

  const renderApiProviderItem = (
    value: string,
    label: string,
    hasKey: boolean,
  ) => (
    <SelectItem value={value} disabled={!hasKey}>
      <span className="flex items-center gap-2">
        {label}
        <Badge
          variant="outline"
          className={cn(
            "ml-1 text-xs",
            hasKey
              ? "border-green-500 text-green-700 dark:text-green-400"
              : "text-muted-foreground",
          )}
        >
          {hasKey ? t("apiKeyConfigured") : t("apiKeyRequired")}
        </Badge>
      </span>
    </SelectItem>
  );

  return (
    <>
      <div className="space-y-6">
        {error && (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {/* Hybrid Search Weights */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Settings className="h-5 w-5" />
              {t("hybridSearchWeights")}
            </CardTitle>
            <CardDescription>{t("hybridSearchWeightsDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label htmlFor="semantic_weight">
                  {t("semanticWeightLabel")}
                </Label>
                <Badge variant="outline">
                  {getCurrentValue("semantic_weight")?.toFixed(2)}
                </Badge>
              </div>
              <Input
                id="semantic_weight"
                type="number"
                step="0.1"
                min="0"
                max="1"
                value={getCurrentValue("semantic_weight")}
                onChange={(e) => handleWeightChange(parseFloat(e.target.value))}
                className="font-mono"
              />
              <p className="text-sm text-muted-foreground">
                {t("semanticWeightDesc")}
              </p>
            </div>

            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label htmlFor="bm25_weight">{t("bm25WeightLabel")}</Label>
                <Badge variant="secondary">
                  {getCurrentValue("bm25_weight")?.toFixed(2)}
                </Badge>
              </div>
              <Input
                id="bm25_weight"
                type="number"
                value={getCurrentValue("bm25_weight")?.toFixed(2)}
                disabled
                className="font-mono bg-muted"
              />
              <p className="text-sm text-muted-foreground">
                {t("bm25WeightDesc")}
              </p>
            </div>

            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label htmlFor="fetch_factor">{t("fetchFactorLabel")}</Label>
                <Badge variant="outline">
                  {getCurrentValue("fetch_factor")}x
                </Badge>
              </div>
              <Input
                id="fetch_factor"
                type="number"
                min="1"
                max="10"
                value={getCurrentValue("fetch_factor")}
                onChange={(e) => {
                  const val = parseInt(e.target.value);
                  if (isNaN(val)) return;
                  setEditedConfig({
                    ...editedConfig,
                    fetch_factor: Math.max(1, Math.min(10, val)),
                  });
                }}
                className="font-mono"
              />
              <p className="text-sm text-muted-foreground">
                {t("fetchFactorDesc")}
                <br />
                {t("fetchFactorExample", {
                  count: getCurrentValue("fetch_factor") * 10,
                })}
              </p>
            </div>

            <div className="rounded-lg bg-blue-50 dark:bg-blue-950 p-4">
              <div className="flex items-start gap-2">
                <Info className="h-4 w-4 text-blue-600 dark:text-blue-400 mt-0.5" />
                <div className="space-y-1">
                  <p className="text-sm font-medium text-blue-900 dark:text-blue-100">
                    {t("impactOnQuality")}
                  </p>
                  <p className="text-xs text-blue-700 dark:text-blue-300">
                    {rerankingDisabledByDeployment
                      ? t("impactOnQualityDescNoRerank")
                      : t("impactOnQualityDesc")}
                  </p>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Reranker Configuration — not rendered when the deployment does
            not rerank (#1580) */}
        {!rerankingDisabledByDeployment && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Sparkles className="h-5 w-5" />
                {t("rerankerConfig")}
              </CardTitle>
              <CardDescription>
                {searchDefaults
                  ? t("rerankerConfigDesc", {
                      provider: providerLabel(searchDefaults.reranker_provider),
                      model: searchDefaults.reranker_model,
                    })
                  : t("rerankerConfigDescPlain")}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              {/* #1646: the tier without reranking gets the gate notice — the
                  tier the matrix names, and the upgrade CTA only where the
                  gate allows one (#1643). Pending renders nothing, and the
                  deployment refusal hides the whole card above (#1580). */}
              <FeatureGateNotice gate={rerank} className="mb-0" />

              {/* #1167: only offer the configure-keys CTA when BYOK is on —
                  with BYOK off the external-keys page cannot add a key, so
                  show the headline without a dangling link. */}
              {rerank.state === "allowed" && !hasAnyRerankerAvailable && (
                <Alert>
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>
                    <p className="font-medium mb-2">{t("noRerankerKeys")}</p>
                    {showConfigureKeysCta && (
                      <p className="text-sm">
                        {/* t.rich with a <link> tag in the message — splitting
                            on an English substring broke non-English locales. */}
                        {t.rich("configureRerankerKeys", {
                          link: (chunks) => (
                            <Link
                              href="/workspace/integrations/external-keys"
                              className="underline font-medium"
                            >
                              {chunks}
                            </Link>
                          ),
                        })}
                      </p>
                    )}
                  </AlertDescription>
                </Alert>
              )}

              <div
                className={cn(
                  "space-y-6",
                  controlsDisabled && "opacity-50 pointer-events-none",
                )}
              >
                <div className="flex items-center justify-between">
                  <div className="space-y-0.5">
                    <Label htmlFor="use_rerank" className="text-base">
                      {t("enableReranking")}
                    </Label>
                    <p className="text-sm text-muted-foreground">
                      {searchDefaults
                        ? t("enableRerankingDesc", {
                            state: searchDefaults.use_rerank
                              ? t("deploymentDefaultOn")
                              : t("deploymentDefaultOff"),
                          })
                        : t("enableRerankingDescPlain")}
                    </p>
                  </div>
                  <Switch
                    id="use_rerank"
                    checked={getCurrentValue("use_rerank")}
                    onCheckedChange={(checked) =>
                      setEditedConfig({ ...editedConfig, use_rerank: checked })
                    }
                    disabled={controlsDisabled}
                  />
                </div>

                {getCurrentValue("use_rerank") && (
                  <>
                    <div className="space-y-3">
                      <Label htmlFor="reranker_provider">{t("provider")}</Label>
                      <Select
                        value={getCurrentValue("reranker_provider")}
                        onValueChange={(value) =>
                          handleProviderChange(
                            value as "voyage" | "cohere" | "self_hosted",
                          )
                        }
                        // pointer-events-none above only stops the mouse; the
                        // Radix trigger stays keyboard-reachable unless disabled.
                        disabled={controlsDisabled}
                      >
                        <SelectTrigger id="reranker_provider">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem
                            value="self_hosted"
                            disabled={!selfHostedAvailable}
                          >
                            <span className="flex items-center gap-2">
                              {t("selfHostedLocal")}
                              <Badge
                                variant="outline"
                                className={cn(
                                  "ml-1 text-xs",
                                  selfHostedAvailable
                                    ? "border-green-500 text-green-700 dark:text-green-400"
                                    : "text-muted-foreground",
                                )}
                              >
                                {selfHostedAvailable
                                  ? t("selfHostedAvailable")
                                  : t("selfHostedUnavailable")}
                              </Badge>
                            </span>
                          </SelectItem>
                          {renderApiProviderItem(
                            "voyage",
                            "Voyage AI",
                            hasVoyageKey,
                          )}
                          {renderApiProviderItem(
                            "cohere",
                            "Cohere",
                            hasCohereKey,
                          )}
                        </SelectContent>
                      </Select>
                      <p className="text-sm text-muted-foreground">
                        {getProviderDescription()}
                      </p>
                      {selectedProviderUnavailable && (
                        <Alert>
                          <AlertCircle className="h-4 w-4" />
                          <AlertDescription>
                            {currentProvider === "self_hosted" ? (
                              <p className="text-sm">
                                {t("selfHostedUnavailableDetail")}
                              </p>
                            ) : showConfigureKeysCta ? (
                              <p className="text-sm">
                                {t.rich("configureRerankerKeys", {
                                  link: (chunks) => (
                                    <Link
                                      href="/workspace/integrations/external-keys"
                                      className="underline font-medium"
                                    >
                                      {chunks}
                                    </Link>
                                  ),
                                })}
                              </p>
                            ) : (
                              // #1167: BYOK off — key setup is not available in
                              // this deployment, so no configure link. #1572:
                              // same when the keyless self_hosted default is up.
                              <p className="text-sm">{t("noRerankerKeys")}</p>
                            )}
                          </AlertDescription>
                        </Alert>
                      )}
                    </div>

                    <div className="space-y-3">
                      <Label htmlFor="reranker_model">{t("model")}</Label>
                      <Select
                        value={getCurrentValue("reranker_model")}
                        onValueChange={(value) =>
                          setEditedConfig({
                            ...editedConfig,
                            reranker_model: value,
                          })
                        }
                        disabled={controlsDisabled}
                      >
                        <SelectTrigger id="reranker_model">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {availableModels.map((model) => (
                            <SelectItem key={model.value} value={model.value}>
                              {model.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <p className="text-sm text-muted-foreground">
                        {currentProvider === "voyage"
                          ? t("voyageBestQuality")
                          : currentProvider === "self_hosted"
                            ? t("qwen3BestQuality")
                            : t("cohereMultilingual")}
                      </p>
                    </div>
                  </>
                )}
              </div>
            </CardContent>
          </Card>
        )}

        {/* Content chunk vectors (#2104) — only with a backend that
            reports them (#2015) */}
        {chunksShown && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Layers className="h-5 w-5" />
                {t("contentChunks.title")}
                {storedChunks &&
                  (chunksInactive ? (
                    <Badge variant="outline" className="text-muted-foreground">
                      {t("contentChunks.inactiveBadge")}
                    </Badge>
                  ) : (
                    <Badge
                      variant="outline"
                      className="border-green-500 text-green-700 dark:text-green-400"
                    >
                      {t("contentChunks.activeBadge")}
                    </Badge>
                  ))}
              </CardTitle>
              <CardDescription>
                {t("contentChunks.description")}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-center justify-between gap-4">
                <div className="space-y-0.5">
                  <Label htmlFor="content_chunks_enabled" className="text-base">
                    {t("contentChunks.toggleLabel")}
                  </Label>
                  <p
                    id="content-chunks-desc"
                    className="text-sm text-muted-foreground"
                  >
                    {t("contentChunks.toggleDesc")}
                  </p>
                </div>
                <Switch
                  id="content_chunks_enabled"
                  checked={currentChunks}
                  onCheckedChange={handleChunksToggle}
                  // Not while saving: a successful save resets the staged
                  // edits, which would drop a change confirmed meanwhile.
                  disabled={chunksSwitchBlocked || saving}
                  aria-describedby={
                    chunksBlockedReason
                      ? "content-chunks-desc content-chunks-reason"
                      : "content-chunks-desc"
                  }
                />
              </div>

              {chunksBlockedReason && (
                <p
                  id="content-chunks-reason"
                  className="text-sm text-muted-foreground"
                >
                  {chunkReasonText(chunksBlockedReason)}
                </p>
              )}

              {chunksInactiveForPlan && (
                <Alert>
                  <Info className="h-4 w-4" />
                  <AlertDescription className="space-y-1">
                    <p className="text-sm">
                      {chunksReason === "cap"
                        ? t("contentChunks.inactiveCap", {
                            inUse: chunksInUse,
                            limit: chunksLimit,
                          })
                        : t("contentChunks.inactivePlan")}
                    </p>
                    <p className="text-sm">{t("contentChunks.inactiveKept")}</p>
                  </AlertDescription>
                </Alert>
              )}

              {chunksBackfillPending > 0 && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  {/* A snapshot, not live progress: no spinner. */}
                  <Info className="h-4 w-4 shrink-0" />
                  <span>
                    {t("contentChunks.backfillPending", {
                      count: chunksBackfillPending,
                    })}
                  </span>
                </div>
              )}
            </CardContent>
          </Card>
        )}

        {/* Recall LLM selector (#2148) — only with a backend that reports
            it (#2144) */}
        {selectorShown && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ListFilter className="h-5 w-5" />
                {t("llmSelector.title")}
                {storedSelector &&
                  (selectorInactive ? (
                    <Badge variant="outline" className="text-muted-foreground">
                      {t("llmSelector.inactiveBadge")}
                    </Badge>
                  ) : (
                    <Badge
                      variant="outline"
                      className="border-green-500 text-green-700 dark:text-green-400"
                    >
                      {t("llmSelector.activeBadge")}
                    </Badge>
                  ))}
              </CardTitle>
              <CardDescription>{t("llmSelector.description")}</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex items-center justify-between gap-4">
                <div className="space-y-0.5">
                  <Label htmlFor="llm_selector_enabled" className="text-base">
                    {t("llmSelector.toggleLabel")}
                  </Label>
                  <p
                    id="llm-selector-desc"
                    className="text-sm text-muted-foreground"
                  >
                    {t("llmSelector.toggleDesc")}
                  </p>
                </div>
                <Switch
                  id="llm_selector_enabled"
                  checked={currentSelector}
                  onCheckedChange={handleSelectorToggle}
                  // Not while saving: a successful save resets the staged
                  // edits, which would drop a change confirmed meanwhile.
                  disabled={selectorSwitchBlocked || saving}
                  aria-describedby={
                    selectorBlockedReason
                      ? "llm-selector-desc llm-selector-reason"
                      : "llm-selector-desc"
                  }
                />
              </div>

              {selectorBlockedReason && (
                <p
                  id="llm-selector-reason"
                  className="text-sm text-muted-foreground"
                >
                  {selectorReasonText(selectorBlockedReason)}
                </p>
              )}

              {selectorInactiveForPlan && (
                <Alert>
                  <Info className="h-4 w-4" />
                  <AlertDescription className="space-y-1">
                    <p className="text-sm">
                      {selectorReason === "cap"
                        ? t("llmSelector.inactiveCap", {
                            inUse: selectorInUse,
                            limit: selectorLimit ?? 0,
                          })
                        : t("llmSelector.inactivePlan")}
                    </p>
                    <p className="text-sm">{t("llmSelector.inactiveNote")}</p>
                  </AlertDescription>
                </Alert>
              )}

              {showSelectorUsage && (
                <p className="text-sm text-muted-foreground">
                  {t("llmSelector.usage", {
                    inUse: selectorInUse,
                    limit: selectorLimit,
                  })}
                </p>
              )}
            </CardContent>
          </Card>
        )}

        {/* Embedding Configuration (Read-only) */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Database className="h-5 w-5" />
              {t("embeddingConfig")}
            </CardTitle>
            <CardDescription>{t("embeddingConfigDesc")}</CardDescription>
          </CardHeader>
          <CardContent>
            <Alert>
              <Info className="h-4 w-4" />
              <AlertDescription>{t("embeddingImmutable")}</AlertDescription>
            </Alert>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
              <div className="p-4 bg-gray-50 dark:bg-gray-900 rounded-lg">
                <p className="text-sm text-muted-foreground mb-1">
                  {t("embeddingModel")}
                </p>
                <p className="font-mono font-semibold">
                  {config.embedding_model || "text-embedding-3-small"}
                </p>
              </div>
              <div className="p-4 bg-gray-50 dark:bg-gray-900 rounded-lg">
                <p className="text-sm text-muted-foreground mb-1">
                  {t("vectorDimensions")}
                </p>
                <p className="font-mono font-semibold">
                  {config.embedding_dimensions || 512}
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* #2104: confirm a content chunk change before it is staged */}
      <AlertDialog open={chunkDialogOpen} onOpenChange={setChunkDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {chunkTarget
                ? t("contentChunks.enableConfirmTitle")
                : t("contentChunks.disableConfirmTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                {chunkTarget ? (
                  <>
                    <p>
                      {config.embedding_provider
                        ? t("contentChunks.enableConfirmBody", {
                            provider: config.embedding_provider,
                          })
                        : t("contentChunks.enableConfirmBodyNoProvider")}
                    </p>
                    {/* Only where the reranker is actually called: the
                        plan and the deployment both allow it (#1645). */}
                    {useRerank && rerank.state === "allowed" && (
                      <p>{t("contentChunks.enableConfirmRerank")}</p>
                    )}
                    <p>{t("contentChunks.enableConfirmBackfill")}</p>
                  </>
                ) : (
                  <p>{t("contentChunks.disableConfirmBody")}</p>
                )}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={confirmChunksToggle}>
              {chunkTarget
                ? t("contentChunks.confirmEnable")
                : t("contentChunks.confirmDisable")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* #2148: confirm turning the LLM selector on before it is staged */}
      <AlertDialog
        open={selectorDialogOpen}
        onOpenChange={setSelectorDialogOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t("llmSelector.enableConfirmTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>{t("llmSelector.enableConfirmBody")}</p>
                <p>{t("llmSelector.enableConfirmPassages")}</p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={confirmSelectorEnable}>
              {t("llmSelector.confirmEnable")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Sticky Save Bar — only rendered when dirty */}
      {isDirty && (
        <div className="fixed bottom-14 left-0 right-0 z-[49] border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="container flex items-center justify-between py-3 px-4 max-w-4xl mx-auto">
            <p className="text-sm text-muted-foreground">
              {cannotSave
                ? t("providerUnavailableCannotSave")
                : t("unsavedChangesBar")}
            </p>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={refreshConfig}>
                {t("discardChanges")}
              </Button>
              <Button
                size="sm"
                onClick={handleSave}
                disabled={saving || !!cannotSave}
              >
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
