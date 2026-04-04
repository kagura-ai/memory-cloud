"use client";

/**
 * Search Settings Page
 *
 * Configure context-level hybrid search and reranker settings.
 * Issue #130 → #160: Context-scoped Search & Reranker Settings UI
 * Issue #223: i18n support
 */

import { useEffect, useState } from "react";
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
import { Badge } from "@/components/ui/badge";
import {
  ArrowLeft,
  Settings,
  Save,
  RefreshCw,
  RotateCcw,
  AlertCircle,
  Info,
  Database,
  Lock,
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
import { useToast } from "@/hooks/use-toast";
import { useParams, useRouter } from "next/navigation";
import { useMemoryContext } from "@/contexts/MemoryContextContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { cn } from "@/styles/design-tokens";

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
  const { toast } = useToast();
  const { currentContext } = useMemoryContext();
  const { currentWorkspace } = useWorkspace();

  const contextId = paramContextId;
  const contextName = context?.display_name || context?.name;

  // Issue #149: Check if Free plan (reranking not available)
  const isFree = currentWorkspace?.plan_name === "free";

  // Fetch context info
  useEffect(() => {
    const fetchContext = async () => {
      try {
        setLoadingContext(true);
        const ctx = await getContext(paramContextId);
        setContext(ctx);
      } catch (err) {
        console.error("Failed to fetch context:", err);
        setError("Failed to load context");
      } finally {
        setLoadingContext(false);
      }
    };

    if (paramContextId) {
      fetchContext();
    }
  }, [paramContextId]);

  useEffect(() => {
    if (contextId && !loadingContext) {
      loadConfig();
      loadExternalKeys();
    }
  }, [contextId, loadingContext]);

  const loadConfig = async () => {
    if (!contextId) return;

    try {
      setLoading(true);
      setError(null);
      const data = await getContextSearchConfig(contextId);
      setConfig(data);
      setEditedConfig({});
    } catch (err: any) {
      const errorMsg = err.response?.data?.detail || t("errorLoad");
      setError(errorMsg);
      toast({
        title: tCommon("error"),
        description: errorMsg,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const loadExternalKeys = async () => {
    try {
      const keys = await listExternalAPIKeys();
      setExternalKeys(keys.filter((k) => k.enabled)); // Only enabled keys
    } catch (err: any) {
      console.error("Failed to load external keys:", err);
      // Don't show error toast - this is optional information
      setExternalKeys([]);
    }
  };

  const handleSave = async () => {
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
      await loadConfig();
    } catch (err: any) {
      const errorMsg = err.response?.data?.detail || t("errorSave");
      setError(errorMsg);
      toast({
        title: tCommon("error"),
        description: errorMsg,
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!contextId) return;

    if (!confirm(t("confirmReset"))) {
      return;
    }

    try {
      setError(null);
      await resetContextSearchConfig(contextId);
      toast({ title: tCommon("success"), description: t("configReset") });
      await loadConfig();
    } catch (err: any) {
      const errorMsg = err.response?.data?.detail || t("errorReset");
      setError(errorMsg);
      toast({
        title: tCommon("error"),
        description: errorMsg,
        variant: "destructive",
      });
    }
  };

  const handleWeightChange = (semantic: number) => {
    setEditedConfig({
      ...editedConfig,
      semantic_weight: semantic,
      bm25_weight: parseFloat((1.0 - semantic).toFixed(2)),
    });
  };

  const handleProviderChange = (provider: "voyage" | "cohere") => {
    const defaultModels = {
      voyage: "rerank-2",
      cohere: "rerank-multilingual-v3.0",
    };
    setEditedConfig({
      ...editedConfig,
      reranker_provider: provider,
      reranker_model: defaultModels[provider],
    });
  };

  const getCurrentValue = <K extends keyof ContextSearchConfig>(
    key: K,
  ): ContextSearchConfig[K] => {
    if (
      key in editedConfig &&
      editedConfig[key as keyof ContextSearchConfigUpdate] !== undefined
    ) {
      return editedConfig[
        key as keyof ContextSearchConfigUpdate
      ] as ContextSearchConfig[K];
    }
    return config?.[key] as ContextSearchConfig[K];
  };

  const hasChanges = Object.keys(editedConfig).length > 0;

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

  // Check which providers have API keys configured
  const hasVoyageKey = externalKeys.some(
    (k) => k.provider.toLowerCase() === "voyage" && k.enabled,
  );
  const hasCohereKey = externalKeys.some(
    (k) => k.provider.toLowerCase() === "cohere" && k.enabled,
  );
  const hasAnyRerankerKey = hasVoyageKey || hasCohereKey;

  const voyageModels = [
    { value: "rerank-2", label: "rerank-2 (Best quality)" },
    { value: "rerank-2-lite", label: "rerank-2-lite (Faster, cheaper)" },
  ];

  const cohereModels = [
    { value: "rerank-multilingual-v3.0", label: "Multilingual v3.0" },
    { value: "rerank-english-v3.0", label: "English v3.0" },
  ];

  const currentProvider = getCurrentValue("reranker_provider");
  const availableModels =
    currentProvider === "voyage" ? voyageModels : cohereModels;

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
            <Button onClick={loadConfig} variant="outline" size="sm">
              <RefreshCw className="h-4 w-4 mr-2" />
              {t("refresh")}
            </Button>
            {hasChanges && (
              <Button onClick={handleSave} disabled={saving} size="sm">
                <Save className="h-4 w-4 mr-2" />
                {saving ? t("saving") : t("saveChanges")}
              </Button>
            )}
          </div>
        }
      />

      {hasChanges && (
        <Alert>
          <Info className="h-4 w-4" />
          <AlertDescription>{t("unsavedChanges")}</AlertDescription>
        </Alert>
      )}

      {error && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Reranker Configuration Card */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="h-5 w-5" />
            {t("rerankerConfig")}
          </CardTitle>
          <CardDescription>{t("rerankerConfigDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Issue #149: Free plan restriction */}
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

          {/* Issue #217: Show API key warning before toggle (not inside use_rerank conditional) */}
          {!isFree && !hasAnyRerankerKey && (
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
              (isFree || !hasAnyRerankerKey) &&
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
                disabled={isFree || !hasAnyRerankerKey}
              />
            </div>

            {getCurrentValue("use_rerank") && (
              <>
                {hasAnyRerankerKey && (
                  <div className="space-y-3">
                    <Label htmlFor="reranker_provider">{t("provider")}</Label>
                    <Select
                      value={getCurrentValue("reranker_provider")}
                      onValueChange={(value) =>
                        handleProviderChange(value as "voyage" | "cohere")
                      }
                    >
                      <SelectTrigger id="reranker_provider">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {hasVoyageKey && (
                          <SelectItem value="voyage">Voyage AI</SelectItem>
                        )}
                        {hasCohereKey && (
                          <SelectItem value="cohere">Cohere</SelectItem>
                        )}
                      </SelectContent>
                    </Select>
                    <p className="text-sm text-muted-foreground">
                      {hasVoyageKey && hasCohereKey
                        ? t("bothAvailable")
                        : hasVoyageKey
                          ? t("voyageConfigured")
                          : t("cohereConfigured")}
                    </p>
                  </div>
                )}

                {hasAnyRerankerKey && (
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
                        : t("cohereMultilingual")}
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Embedding Configuration (Read-only) */}
      {config && (
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
      )}

      {/* Current Configuration Summary */}
      <Card>
        <CardHeader>
          <CardTitle>{t("configSummary")}</CardTitle>
          <CardDescription>{t("configSummaryDesc")}</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
            <div>
              <p className="text-muted-foreground">{t("semanticWeight")}</p>
              <p className="font-mono font-semibold">
                {getCurrentValue("semantic_weight")?.toFixed(2)}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">{t("bm25Weight")}</p>
              <p className="font-mono font-semibold">
                {getCurrentValue("bm25_weight")?.toFixed(2)}
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">{t("fetchFactor")}</p>
              <p className="font-mono font-semibold">
                {getCurrentValue("fetch_factor")}x
              </p>
            </div>
            <div>
              <p className="text-muted-foreground">{t("reranking")}</p>
              <p className="font-semibold">
                {getCurrentValue("use_rerank") ? (
                  <span className="text-green-600">{t("enabled")}</span>
                ) : (
                  <span className="text-gray-500">{t("disabled")}</span>
                )}
              </p>
            </div>
            {getCurrentValue("use_rerank") && (
              <>
                <div>
                  <p className="text-muted-foreground">{t("provider")}</p>
                  <p className="font-semibold capitalize">
                    {getCurrentValue("reranker_provider")}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground">{t("model")}</p>
                  <p className="font-mono text-xs">
                    {getCurrentValue("reranker_model")}
                  </p>
                </div>
              </>
            )}
          </div>

          {config && (
            <div className="mt-4 pt-4 border-t text-xs text-muted-foreground">
              <p>
                {t("lastUpdated", {
                  date: new Date(config.updated_at).toLocaleString(),
                })}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Hybrid Search Weights Card */}
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
              onChange={(e) =>
                setEditedConfig({
                  ...editedConfig,
                  fetch_factor: parseInt(e.target.value),
                })
              }
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

      {/* Actions */}
      <div className="flex justify-between items-center">
        <Button onClick={handleReset} variant="outline">
          <RotateCcw className="h-4 w-4 mr-2" />
          {t("resetToDefaults")}
        </Button>
        {hasChanges && (
          <Button onClick={handleSave} disabled={saving}>
            <Save className="h-4 w-4 mr-2" />
            {saving ? t("saving") : t("saveChanges")}
          </Button>
        )}
      </div>
    </PageContainer>
  );
}
