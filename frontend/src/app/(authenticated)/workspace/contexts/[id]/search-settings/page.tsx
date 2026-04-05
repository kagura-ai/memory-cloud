"use client";

/**
 * Search Settings Page
 *
 * Configure context-level hybrid search and reranker settings.
 * Issue #130 / #160: Context-scoped Search & Reranker Settings UI
 * Issue #158: Unified UX with context settings pattern
 * Issue #157: Ollama reranker support
 * Issue #223: i18n support
 */

import { useEffect, useState, useCallback } from "react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { SpinnerLoading } from "@/components/common/LoadingState";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft,
  Settings,
  Save,
  RotateCcw,
  AlertCircle,
  Info,
  Database,
  Lock,
  Loader2,
  Sparkles,
} from "lucide-react";
import {
  getContextSearchConfig,
  updateContextSearchConfig,
  resetContextSearchConfig,
  type ContextSearchConfig,
  type ContextSearchConfigUpdate,
} from "@/lib/api/contexts";
import {
  listExternalAPIKeys,
  type ExternalAPIKey,
} from "@/lib/api/external-keys";
import { apiClient } from "@/lib/api/base";
import { useToast } from "@/hooks/use-toast";
import { useParams, useRouter } from "next/navigation";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { cn } from "@/styles/design-tokens";

interface TelemetryServiceStatus {
  status: string;
  details?: Record<string, unknown>;
}

interface TelemetryResponse {
  services: Record<string, TelemetryServiceStatus>;
}

// Static model lists — no runtime deps, defined once at module level
const VOYAGE_MODELS = [
  { value: "rerank-2", label: "rerank-2 (Best quality)" },
  { value: "rerank-2-lite", label: "rerank-2-lite (Faster, cheaper)" },
];

const COHERE_MODELS = [
  { value: "rerank-multilingual-v3.0", label: "Multilingual v3.0" },
  { value: "rerank-english-v3.0", label: "English v3.0" },
];

const OLLAMA_MODELS = [
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
  ollama: "dengcao/Qwen3-Reranker-8B:Q5_K_M",
};

function getErrorMessage(err: unknown, fallback: string): string {
  return (err as { message?: string })?.message ?? fallback;
}

export default function SearchSettingsPage() {
  const t = useTranslations("searchSettings");
  const tCommon = useTranslations("common");

  const params = useParams();
  const router = useRouter();
  const paramContextId = params.id as string;

  const [context, setContext] = useState<Context | null>(null);
  const [loadingContext, setLoadingContext] = useState(true);
  const [config, setConfig] = useState<ContextSearchConfig | null>(null);
  const [editedConfig, setEditedConfig] = useState<
    Partial<ContextSearchConfigUpdate>
  >({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [externalKeys, setExternalKeys] = useState<ExternalAPIKey[]>([]);
  const [ollamaAvailable, setOllamaAvailable] = useState(false);
  const [resetDialogOpen, setResetDialogOpen] = useState(false);
  const { toast } = useToast();
  const { currentWorkspace } = useWorkspace();

  const contextId = paramContextId;
  const contextName = context?.display_name || context?.name;
  const isFree = currentWorkspace?.plan_name === "free";
  const isDirty = Object.keys(editedConfig).length > 0;

  const loadExternalKeys = useCallback(async () => {
    try {
      const keys = await listExternalAPIKeys();
      setExternalKeys(keys.filter((k) => k.enabled));
    } catch {
      setExternalKeys([]);
    }
  }, []);

  const loadTelemetry = useCallback(async () => {
    try {
      const telemetry = await apiClient.get<TelemetryResponse>(
        "/api/v1/system/telemetry",
      );
      setOllamaAvailable(telemetry.services?.ollama?.status === "ok");
    } catch {
      setOllamaAvailable(false);
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
      const errorMsg = getErrorMessage(err, t("errorLoad"));
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
      // Silent refresh — don't show error
    }
  }, [contextId]);

  useEffect(() => {
    const fetchContext = async () => {
      try {
        setLoadingContext(true);
        const ctx = await getContext(paramContextId);
        setContext(ctx);
      } catch {
        setError(t("failedToLoad"));
      } finally {
        setLoadingContext(false);
      }
    };

    if (paramContextId) {
      fetchContext();
    }
  }, [paramContextId]);

  useEffect(() => {
    if (!contextId || loadingContext) return;
    Promise.all([loadConfig(), loadExternalKeys(), loadTelemetry()]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contextId, loadingContext]);

  const handleSave = useCallback(async () => {
    if (!contextId || Object.keys(editedConfig).length === 0) return;

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
      };

      await updateContextSearchConfig(contextId, updateData);
      toast({ title: tCommon("success"), description: t("configSaved") });
      await refreshConfig();
    } catch (err: unknown) {
      const errorMsg = getErrorMessage(err, t("errorSave"));
      setError(errorMsg);
      toast({
        title: tCommon("error"),
        description: errorMsg,
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  }, [contextId, editedConfig, config, t, tCommon, toast, refreshConfig]);

  const handleReset = useCallback(async () => {
    if (!contextId) return;

    try {
      setError(null);
      await resetContextSearchConfig(contextId);
      toast({ title: tCommon("success"), description: t("configReset") });
      await refreshConfig();
    } catch (err: unknown) {
      const errorMsg = getErrorMessage(err, t("errorReset"));
      setError(errorMsg);
      toast({
        title: tCommon("error"),
        description: errorMsg,
        variant: "destructive",
      });
    }
  }, [contextId, t, tCommon, toast, refreshConfig]);

  const handleWeightChange = (semantic: number) => {
    if (isNaN(semantic)) return;
    const clamped = Math.max(0, Math.min(1, semantic));
    setEditedConfig({
      ...editedConfig,
      semantic_weight: clamped,
      bm25_weight: parseFloat((1.0 - clamped).toFixed(2)),
    });
  };

  const handleProviderChange = (provider: "voyage" | "cohere" | "ollama") => {
    setEditedConfig({
      ...editedConfig,
      reranker_provider: provider,
      reranker_model: DEFAULT_RERANKER_MODELS[provider],
    });
  };

  const getCurrentValue = <K extends keyof ContextSearchConfig>(
    key: K,
  ): ContextSearchConfig[K] => {
    return (
      (editedConfig[key as keyof ContextSearchConfigUpdate] as
        | ContextSearchConfig[K]
        | undefined) ?? (config?.[key] as ContextSearchConfig[K])
    );
  };

  if (loadingContext || loading) {
    return (
      <PageContainer>
        <SpinnerLoading message={t("loadingConfig")} />
      </PageContainer>
    );
  }

  if (!contextId) {
    return (
      <PageContainer>
        <Alert>
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{t("noContextSelected")}</AlertDescription>
        </Alert>
      </PageContainer>
    );
  }

  if (!config) {
    return (
      <PageContainer>
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error || t("failedToLoad")}</AlertDescription>
        </Alert>
      </PageContainer>
    );
  }

  const hasVoyageKey = externalKeys.some(
    (k) => k.provider.toLowerCase() === "voyage" && k.enabled,
  );
  const hasCohereKey = externalKeys.some(
    (k) => k.provider.toLowerCase() === "cohere" && k.enabled,
  );
  const hasAnyRerankerAvailable =
    hasVoyageKey || hasCohereKey || ollamaAvailable;

  const currentProvider = getCurrentValue("reranker_provider");
  const availableModels =
    currentProvider === "voyage"
      ? VOYAGE_MODELS
      : currentProvider === "ollama"
        ? OLLAMA_MODELS
        : COHERE_MODELS;

  const selectedProviderUnavailable =
    (currentProvider === "voyage" && !hasVoyageKey) ||
    (currentProvider === "cohere" && !hasCohereKey) ||
    (currentProvider === "ollama" && !ollamaAvailable);

  const useRerank = getCurrentValue("use_rerank");
  const cannotSave = isDirty && useRerank && selectedProviderUnavailable;

  const getProviderDescription = () => {
    if (ollamaAvailable && hasVoyageKey && hasCohereKey)
      return t("allProvidersAvailable");
    if (ollamaAvailable && hasVoyageKey) return t("ollamaAndVoyage");
    if (ollamaAvailable && hasCohereKey) return t("ollamaAndCohere");
    if (ollamaAvailable) return t("ollamaOnly");
    if (hasVoyageKey && hasCohereKey) return t("bothAvailable");
    if (hasVoyageKey) return t("voyageConfigured");
    if (hasCohereKey) return t("cohereConfigured");
    return "";
  };

  // Helper to render API-key-based provider SelectItem (Voyage / Cohere)
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

  const pageTitle = contextName
    ? t("titleWithContext", { contextName })
    : t("title");

  return (
    <PageContainer>
      <PageHeader
        title={pageTitle}
        description={t("description")}
        actions={
          <div className="flex gap-2">
            <Button
              onClick={() => router.push("/workspace/contexts")}
              variant="outline"
              size="sm"
            >
              <ArrowLeft className="h-4 w-4 mr-2" />
              {tCommon("back")}
            </Button>
          </div>
        }
      />

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
                  {t("impactOnQualityDesc")}
                </p>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Reranker Configuration */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5" />
            {t("rerankerConfig")}
          </CardTitle>
          <CardDescription>{t("rerankerConfigDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {isFree && (
            <Alert>
              <Lock className="h-4 w-4" />
              <AlertDescription>
                <p className="font-medium mb-1">
                  {t("rerankerNotAvailableFree")}
                </p>
                <p className="text-sm">
                  {t("upgradeToBasic").split("Basic plan")[0]}
                  <Link
                    href="/workspace/plan"
                    className="underline font-medium"
                  >
                    Basic plan
                  </Link>
                  {t("upgradeToBasic").split("Basic plan")[1]}
                </p>
              </AlertDescription>
            </Alert>
          )}

          {!isFree && !hasAnyRerankerAvailable && (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                <p className="font-medium mb-2">{t("noRerankerKeys")}</p>
                <p className="text-sm">
                  {t("configureRerankerKeys").split("External API Keys")[0]}
                  <Link
                    href="/workspace/settings/external-keys"
                    className="underline font-medium"
                  >
                    External API Keys
                  </Link>
                  {t("configureRerankerKeys").split("External API Keys")[1]}
                </p>
              </AlertDescription>
            </Alert>
          )}

          <div
            className={cn(
              "space-y-6",
              (isFree || !hasAnyRerankerAvailable) &&
                "opacity-50 pointer-events-none",
            )}
          >
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="use_rerank" className="text-base">
                  {t("enableReranking")}
                </Label>
                <p className="text-sm text-muted-foreground">
                  {t("enableRerankingDesc")}
                </p>
              </div>
              <Switch
                id="use_rerank"
                checked={getCurrentValue("use_rerank")}
                onCheckedChange={(checked) =>
                  setEditedConfig({ ...editedConfig, use_rerank: checked })
                }
                disabled={isFree || !hasAnyRerankerAvailable}
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
                        value as "voyage" | "cohere" | "ollama",
                      )
                    }
                  >
                    <SelectTrigger id="reranker_provider">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="ollama" disabled={!ollamaAvailable}>
                        <span className="flex items-center gap-2">
                          {t("ollamaLocal")}
                          <Badge
                            variant="outline"
                            className={cn(
                              "ml-1 text-xs",
                              ollamaAvailable
                                ? "border-green-500 text-green-700 dark:text-green-400"
                                : "text-muted-foreground",
                            )}
                          >
                            {ollamaAvailable
                              ? t("ollamaAvailable")
                              : t("ollamaUnavailable")}
                          </Badge>
                        </span>
                      </SelectItem>
                      {renderApiProviderItem(
                        "voyage",
                        "Voyage AI",
                        hasVoyageKey,
                      )}
                      {renderApiProviderItem("cohere", "Cohere", hasCohereKey)}
                    </SelectContent>
                  </Select>
                  <p className="text-sm text-muted-foreground">
                    {getProviderDescription()}
                  </p>
                  {selectedProviderUnavailable && (
                    <Alert>
                      <AlertCircle className="h-4 w-4" />
                      <AlertDescription>
                        {currentProvider === "ollama" ? (
                          <p className="text-sm">
                            {t("ollamaUnavailableDetail")}
                          </p>
                        ) : (
                          <p className="text-sm">
                            {
                              t("configureRerankerKeys").split(
                                "External API Keys",
                              )[0]
                            }
                            <Link
                              href="/workspace/settings/external-keys"
                              className="underline font-medium"
                            >
                              External API Keys
                            </Link>
                            {
                              t("configureRerankerKeys").split(
                                "External API Keys",
                              )[1]
                            }
                          </p>
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
                      : currentProvider === "ollama"
                        ? t("qwen3BestQuality")
                        : t("cohereMultilingual")}
                  </p>
                </div>
              </>
            )}
          </div>
        </CardContent>
      </Card>

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

      {/* Reset to Defaults */}
      <div className="flex justify-start">
        <Button onClick={() => setResetDialogOpen(true)} variant="outline">
          <RotateCcw className="h-4 w-4 mr-2" />
          {t("resetToDefaults")}
        </Button>
      </div>

      <AlertDialog open={resetDialogOpen} onOpenChange={setResetDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("resetToDefaults")}</AlertDialogTitle>
            <AlertDialogDescription>{t("confirmReset")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setResetDialogOpen(false);
                handleReset();
              }}
            >
              {t("resetToDefaults")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Sticky Save Bar */}
      <div
        className={cn(
          "fixed bottom-0 left-0 right-0 z-50 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60 transition-transform duration-200",
          isDirty ? "translate-y-0" : "translate-y-full",
        )}
      >
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
              disabled={saving || cannotSave}
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
    </PageContainer>
  );
}
