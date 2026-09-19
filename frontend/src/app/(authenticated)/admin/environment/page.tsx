"use client";

/**
 * Environment Configuration Page
 *
 * Display the effective environment configuration values.
 * Admin-only, read-only page: every key is set via environment variables and
 * applied on restart/redeploy, so there is nothing to edit or save (#1580).
 * Issue #46: Environment page implementation
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import {
  SpinnerLoading,
  InlineSpinner,
} from "@/components/common/LoadingState";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  RefreshCw,
  AlertCircle,
  Eye,
  EyeOff,
  Info,
  CheckCircle,
  XCircle,
  Server,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { apiClient } from "@/lib/api";

interface ConfigValue {
  value: string | number | boolean;
  type: "string" | "number" | "boolean" | "enum";
  sensitive: boolean;
  // #1580: env-backed keys cannot be changed here. Absent on older backends,
  // which this page treats the same way — it has no write path.
  readOnly: boolean;
  category: string;
  description?: string;

  // Schema metadata (from /api/v1/config/schema)
  enum_values?: string[];
  enum_descriptions?: Record<string, string>;
  min_value?: number;
  max_value?: number;
  impact?: string;
  examples?: string[];
  recommended?: string;
  requires_restart?: boolean;
  documentation_url?: string;
}

interface ConfigData {
  [key: string]: ConfigValue;
}

export default function EnvironmentPage() {
  const t = useTranslations("admin.environment");

  const [config, setConfig] = useState<ConfigData>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSensitive, setShowSensitive] = useState<Record<string, boolean>>(
    {},
  );
  const [telemetry, setTelemetry] = useState<Record<string, any> | null>(null);
  const [registryModels, setRegistryModels] = useState<
    Array<{
      name: string;
      dimensions: number;
      provider: string;
      available: boolean;
    }>
  >([]);

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    try {
      setLoading(true);

      // Fetch config values, schema, telemetry, and embedding models in parallel
      const [
        configResponse,
        schemaResponse,
        telemetryResponse,
        embeddingResponse,
      ] = await Promise.all([
        apiClient.get<{
          configs: Array<{
            key: string;
            value: any;
            category: string;
            description: string | null;
            is_sensitive: boolean;
            read_only?: boolean;
          }>;
          total: number;
        }>("/api/v1/config?mask_sensitive=true"),
        apiClient.get<Record<string, any>>("/api/v1/config/schema"),
        apiClient
          .get<Record<string, any>>("/api/v1/system/telemetry")
          .catch(() => null),
        apiClient
          .get<{
            models: Array<{
              name: string;
              dimensions: number;
              provider: string;
              available: boolean;
            }>;
            default_model: string;
          }>("/api/v1/system/embedding/models")
          .catch(() => null),
      ]);
      setTelemetry(telemetryResponse);
      if (embeddingResponse) {
        setRegistryModels(embeddingResponse.models);
      }

      // Transform API response to ConfigData format with schema metadata
      const configData: ConfigData = {};
      configResponse.configs.forEach((item) => {
        const value = item.value ?? ""; // Use empty string for null values
        const schema = schemaResponse[item.key];

        configData[item.key] = {
          value: value,
          type:
            schema?.type ||
            (typeof value === "boolean"
              ? "boolean"
              : typeof value === "number"
                ? "number"
                : "string"),
          sensitive: item.is_sensitive,
          readOnly: item.read_only ?? true,
          category: item.category,
          description: schema?.description || item.description || undefined,

          // Add schema metadata
          enum_values: schema?.enum_values,
          enum_descriptions: schema?.enum_descriptions,
          min_value: schema?.min_value,
          max_value: schema?.max_value,
          impact: schema?.impact,
          examples: schema?.examples,
          recommended: schema?.recommended,
          requires_restart: schema?.requires_restart,
          documentation_url: schema?.documentation_url,
        };
      });

      setConfig(configData);
      setError(null);
    } catch (err) {
      console.error("Failed to load config:", err);
      setError(err instanceof Error ? err.message : t("messages.loadError"));
    } finally {
      setLoading(false);
    }
  };

  const toggleSensitive = (key: string) => {
    setShowSensitive({ ...showSensitive, [key]: !showSensitive[key] });
  };

  const groupByCategory = () => {
    const grouped: Record<string, Array<[string, ConfigValue]>> = {};

    Object.entries(config).forEach(([key, value]) => {
      const category = value.category || "other";
      if (!grouped[category]) {
        grouped[category] = [];
      }
      grouped[category].push([key, value]);
    });

    return grouped;
  };

  // Static display only (#1580): no Switch/Input, the values are not editable.
  const renderConfigValue = (key: string, configValue: ConfigValue) => {
    const currentValue = configValue.value;
    const isSensitive = configValue.sensitive;
    const isHidden = isSensitive && !showSensitive[key];

    if (configValue.type === "boolean") {
      const isEnabled = currentValue === true;
      return (
        <div className="flex items-center gap-2">
          {isEnabled ? (
            <CheckCircle className="h-4 w-4 text-green-500" />
          ) : (
            <XCircle className="h-4 w-4 text-gray-400" />
          )}
          <span className="text-sm">
            {isEnabled ? t("messages.enabled") : t("messages.disabled")}
          </span>
        </div>
      );
    }

    if (isSensitive) {
      return (
        <div className="flex items-center gap-2">
          <span className="flex-1 p-2 bg-gray-50 dark:bg-gray-800 rounded border dark:border-gray-700 font-mono text-sm">
            {isHidden ? "••••••••" : currentValue}
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => toggleSensitive(key)}
          >
            {isHidden ? (
              <Eye className="h-4 w-4" />
            ) : (
              <EyeOff className="h-4 w-4" />
            )}
          </Button>
        </div>
      );
    }

    if (configValue.type === "enum" && configValue.enum_values) {
      // Read-only ENUM display with valid values list
      return (
        <div className="space-y-2">
          <div className="flex items-center gap-2 p-2 bg-gray-50 dark:bg-gray-800 rounded border dark:border-gray-700">
            <span className="font-mono text-sm font-medium">
              {currentValue}
            </span>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {configValue.enum_descriptions?.[currentValue as string] || ""}
            </span>
          </div>
          <div className="text-xs text-gray-600 dark:text-gray-400">
            <span className="font-medium">{t("messages.enumValid")}</span>{" "}
            {configValue.enum_values.map((val, idx) => (
              <span key={val}>
                <code className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded">
                  {val}
                </code>
                {idx < configValue.enum_values!.length - 1 && ", "}
              </span>
            ))}
          </div>
        </div>
      );
    }

    const staticValue = (
      <div className="p-2 bg-gray-50 dark:bg-gray-800 rounded border dark:border-gray-700">
        {currentValue === "" ? (
          <span className="text-sm text-gray-400">{t("messages.notSet")}</span>
        ) : (
          <span className="font-mono text-sm font-medium break-all">
            {currentValue}
          </span>
        )}
      </div>
    );

    if (configValue.type === "number") {
      return (
        <div className="space-y-1">
          {staticValue}
          {(configValue.min_value !== undefined ||
            configValue.max_value !== undefined) && (
            <p className="text-xs text-gray-500">
              {t("messages.rangeValid", {
                min: configValue.min_value ?? "−∞",
                max: configValue.max_value ?? "+∞",
              })}
            </p>
          )}
        </div>
      );
    }

    return staticValue;
  };

  const getCategoryIcon = (category: string) => {
    const icons: Record<string, string> = {
      neural_memory: "🧠",
      embedding: "📊",
      search: "🔍",
      memory: "💾",
      system: "⚙️",
      hosted: "☁️",
    };
    return icons[category] || "📁";
  };

  const getCategoryTitle = (category: string) => {
    const titles: Record<string, string> = {
      neural_memory: t("sections.neuralMemory"),
      embedding: t("sections.embedding"),
      search: t("sections.search"),
      memory: t("sections.memory"),
      system: t("sections.system"),
      hosted: t("sections.hosted"),
    };
    return titles[category] || category;
  };

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} description={t("loadingDesc")} />
        <SpinnerLoading size="lg" message={t("messages.loading")} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title={t("title")} description={t("configManagement")} />
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </PageContainer>
    );
  }

  const groupedConfig = groupByCategory();

  return (
    <PageContainer>
      <div className="flex items-start justify-between mb-6">
        <PageHeader title={t("titleFull")} description={t("description")} />

        <div className="flex gap-2">
          <Button onClick={loadConfig} variant="outline" disabled={loading}>
            {loading ? (
              <InlineSpinner size="sm" className="mr-2" />
            ) : (
              <RefreshCw className="h-4 w-4 mr-2" />
            )}
            {t("actions.refresh")}
          </Button>
        </div>
      </div>

      <Alert className="mb-6">
        <Info className="h-4 w-4" />
        <AlertDescription>{t("readOnlyNotice")}</AlertDescription>
      </Alert>

      <div className="space-y-6">
        {/* System Status — Embedding & Services */}
        {telemetry && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Server className="h-5 w-5" />
                {t("systemStatus.title")}
              </CardTitle>
              <CardDescription>{t("systemStatus.description")}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {/* Embedding Config */}
                <div className="p-4 border dark:border-gray-700 rounded-lg space-y-2">
                  <div className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    {t("systemStatus.embeddingConfig")}
                  </div>
                  {telemetry.embedding_config ? (
                    <div className="space-y-1 text-sm">
                      <div className="flex justify-between">
                        <span className="text-gray-500">
                          {t("systemStatus.provider")}
                        </span>
                        <code className="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-xs">
                          {telemetry.embedding_config.provider}
                        </code>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-500">
                          {t("systemStatus.model")}
                        </span>
                        <code className="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-xs">
                          {telemetry.embedding_config.model}
                        </code>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-500">
                          {t("systemStatus.dimensions")}
                        </span>
                        <span className="text-xs">
                          {telemetry.embedding_config.dimensions}
                        </span>
                      </div>
                    </div>
                  ) : (
                    <span className="text-xs text-gray-400">—</span>
                  )}
                </div>

                {/* Self-hosted Status + Embedding Models */}
                <div className="p-4 border dark:border-gray-700 rounded-lg space-y-2">
                  <div className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    {t("systemStatus.self_hosted")}
                  </div>
                  {telemetry.services?.self_hosted ? (
                    <div className="space-y-3">
                      <div className="flex items-center gap-2">
                        {telemetry.services.self_hosted.status === "ok" ? (
                          <CheckCircle className="h-4 w-4 text-green-500" />
                        ) : telemetry.services.self_hosted.status ===
                          "not_configured" ? (
                          <XCircle className="h-4 w-4 text-gray-400" />
                        ) : (
                          <XCircle className="h-4 w-4 text-red-500" />
                        )}
                        <span className="text-sm">
                          {telemetry.services.self_hosted.status}
                        </span>
                      </div>
                      {telemetry.services.self_hosted.status === "ok" &&
                        registryModels.length > 0 && (
                          <div className="space-y-1">
                            <div className="text-xs font-medium text-gray-500 dark:text-gray-400">
                              {t("systemStatus.embeddingModelsTitle")}
                            </div>
                            {registryModels
                              .filter((m) => m.provider === "self_hosted")
                              .map((model) => {
                                const installedModels: string[] =
                                  telemetry.services?.self_hosted?.details
                                    ?.models || [];
                                const isInstalled = installedModels.some(
                                  (m: string) => m.startsWith(model.name),
                                );
                                return (
                                  <div
                                    key={model.name}
                                    className="flex items-center justify-between text-xs"
                                  >
                                    <code className="font-mono">
                                      {model.name}
                                    </code>
                                    {isInstalled ? (
                                      <span className="flex items-center gap-1 text-green-600 dark:text-green-400">
                                        <CheckCircle className="h-3 w-3" />{" "}
                                        {t("systemStatus.installed")}
                                      </span>
                                    ) : (
                                      <span className="flex items-center gap-1 text-gray-400">
                                        <XCircle className="h-3 w-3" />{" "}
                                        {t("systemStatus.notInstalled")}
                                      </span>
                                    )}
                                  </div>
                                );
                              })}
                          </div>
                        )}
                    </div>
                  ) : (
                    <div className="flex items-center gap-2">
                      <XCircle className="h-4 w-4 text-gray-400" />
                      <span className="text-sm text-gray-400">
                        {t("systemStatus.selfHostedNotConfigured")}
                      </span>
                    </div>
                  )}
                </div>

                {/* Qdrant Collections */}
                <div className="p-4 border dark:border-gray-700 rounded-lg space-y-2">
                  <div className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    {t("systemStatus.qdrantCollections")}
                  </div>
                  {telemetry.services?.qdrant?.status === "ok" ? (
                    telemetry.services.qdrant.details?.collection_names
                      ?.length > 0 ? (
                      <div className="space-y-1">
                        {telemetry.services.qdrant.details.collection_names.map(
                          (name: string) => (
                            <div key={name} className="flex items-center gap-2">
                              <div className="w-2 h-2 rounded-full bg-green-500" />
                              <code className="text-xs font-mono">{name}</code>
                            </div>
                          ),
                        )}
                      </div>
                    ) : (
                      <div className="flex items-center gap-2">
                        <CheckCircle className="h-4 w-4 text-green-500" />
                        <span className="text-sm text-gray-500">
                          {t("systemStatus.qdrantNoCollections")}
                        </span>
                      </div>
                    )
                  ) : (
                    <div className="flex items-center gap-2">
                      <XCircle className="h-4 w-4 text-red-500" />
                      <span className="text-sm text-gray-500">
                        {telemetry.services?.qdrant?.details?.error ||
                          t("systemStatus.qdrantError")}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        )}

        {Object.entries(groupedConfig).map(([category, items]) => (
          <Card key={category}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <span>{getCategoryIcon(category)}</span>
                {getCategoryTitle(category)}
              </CardTitle>
              <CardDescription>
                {t("messages.configParams", {
                  count: items.length,
                  s: items.length !== 1 ? "s" : "",
                })}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                {items.map(([key, configValue]) => (
                  <div
                    key={key}
                    data-config-key={key}
                    className="space-y-3 p-4 border dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900"
                  >
                    {/* Header with key name and badges */}
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Label
                          htmlFor={key}
                          className="font-mono text-sm font-semibold"
                        >
                          {key}
                        </Label>
                        {configValue.requires_restart && (
                          <Badge variant="destructive" className="text-xs">
                            {t("actions.requiresRestart")}
                          </Badge>
                        )}
                        {configValue.type === "enum" && (
                          <Badge variant="outline" className="text-xs">
                            {t("messages.typeEnum")}
                          </Badge>
                        )}
                        {configValue.readOnly && (
                          <Badge variant="outline" className="text-xs">
                            {t("actions.readOnly")}
                          </Badge>
                        )}
                      </div>
                    </div>

                    {/* Description */}
                    {configValue.description && (
                      <p className="text-sm text-gray-600 dark:text-gray-400">
                        {configValue.description}
                      </p>
                    )}

                    {/* Value (static) */}
                    <div>{renderConfigValue(key, configValue)}</div>

                    {/* Extended metadata panel */}
                    {(configValue.impact ||
                      configValue.recommended ||
                      configValue.examples) && (
                      <div className="mt-3 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-md space-y-2">
                        {configValue.impact && (
                          <div className="flex items-start gap-2">
                            <Info className="h-4 w-4 text-blue-600 dark:text-blue-400 mt-0.5 flex-shrink-0" />
                            <div>
                              <p className="text-xs font-medium text-blue-900 dark:text-blue-100">
                                {t("metadata.impact")}
                              </p>
                              <p className="text-xs text-blue-700 dark:text-blue-300">
                                {configValue.impact}
                              </p>
                            </div>
                          </div>
                        )}
                        {configValue.recommended && (
                          <div className="text-xs">
                            <span className="font-medium text-green-900 dark:text-green-100">
                              {t("metadata.recommended")}
                            </span>{" "}
                            <span className="text-green-700 dark:text-green-300">
                              {configValue.recommended}
                            </span>
                          </div>
                        )}
                        {configValue.examples &&
                          configValue.examples.length > 0 && (
                            <div className="text-xs">
                              <span className="font-medium text-gray-700 dark:text-gray-300">
                                {t("metadata.examples")}
                              </span>{" "}
                              {configValue.examples.map((ex, idx) => (
                                <span key={idx}>
                                  <code className="px-1 py-0.5 bg-white dark:bg-gray-800 rounded text-gray-800 dark:text-gray-200">
                                    {ex}
                                  </code>
                                  {idx < configValue.examples!.length - 1 &&
                                    ", "}
                                </span>
                              ))}
                            </div>
                          )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </PageContainer>
  );
}
