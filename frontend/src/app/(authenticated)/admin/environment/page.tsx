'use client';

/**
 * Environment Configuration Page
 *
 * Display and manage .env configuration values.
 * Admin-only page with inline editing capability.
 * Issue #46: Environment page implementation
 */

import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { PageContainer } from '@/components/common/PageContainer';
import { PageHeader } from '@/components/common/PageHeader';
import { SpinnerLoading, InlineSpinner } from '@/components/common/LoadingState';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Settings, Save, RefreshCw, AlertCircle, Eye, EyeOff, Info, ExternalLink, CheckCircle, XCircle, Server } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { apiClient } from '@/lib/api';
import { useToast } from '@/hooks/use-toast';

interface ConfigValue {
  value: string | number | boolean;
  type: 'string' | 'number' | 'boolean' | 'enum';
  sensitive: boolean;
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
  const t = useTranslations('admin.environment');
  const tCommon = useTranslations('admin.common');

  const [config, setConfig] = useState<ConfigData>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editedValues, setEditedValues] = useState<Record<string, any>>({});
  const [showSensitive, setShowSensitive] = useState<Record<string, boolean>>({});
  const [telemetry, setTelemetry] = useState<Record<string, any> | null>(null);
  const { toast } = useToast();

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    try {
      setLoading(true);

      // Fetch config values, schema, and telemetry in parallel
      const [configResponse, schemaResponse, telemetryResponse] = await Promise.all([
        apiClient.get<{
          configs: Array<{
            key: string;
            value: any;
            category: string;
            description: string | null;
            is_sensitive: boolean;
          }>;
          total: number;
        }>('/api/v1/config?mask_sensitive=true'),
        apiClient.get<Record<string, any>>('/api/v1/config/schema'),
        apiClient.get<Record<string, any>>('/api/v1/system/telemetry').catch(() => null),
      ]);
      setTelemetry(telemetryResponse);

      // Transform API response to ConfigData format with schema metadata
      const configData: ConfigData = {};
      configResponse.configs.forEach((item) => {
        const value = item.value ?? ''; // Use empty string for null values
        const schema = schemaResponse[item.key];

        configData[item.key] = {
          value: value,
          type: schema?.type || (typeof value === 'boolean' ? 'boolean' : typeof value === 'number' ? 'number' : 'string'),
          sensitive: item.is_sensitive,
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
      console.error('Failed to load config:', err);
      setError(err instanceof Error ? err.message : 'Failed to load configuration');
    } finally {
      setLoading(false);
    }
  };

  const handleValueChange = (key: string, value: any) => {
    setEditedValues({ ...editedValues, [key]: value });
  };

  const handleSave = async () => {
    if (Object.keys(editedValues).length === 0) {
      toast({
        title: t('messages.noChanges'),
        description: t('messages.noChangesDesc'),
      });
      return;
    }

    try {
      setSaving(true);

      // Batch update
      await apiClient.post('/api/v1/config/batch', { updates: editedValues });

      toast({
        title: t('messages.saveSuccess'),
        description: t('messages.saveSuccessDesc', { count: Object.keys(editedValues).length }),
      });

      // Reload config
      await loadConfig();
      setEditedValues({});
    } catch (err) {
      console.error('Failed to save config:', err);
      toast({
        title: t('messages.saveError'),
        description: err instanceof Error ? err.message : t('messages.saveErrorDesc'),
        variant: 'destructive',
      });
    } finally {
      setSaving(false);
    }
  };

  const toggleSensitive = (key: string) => {
    setShowSensitive({ ...showSensitive, [key]: !showSensitive[key] });
  };

  const groupByCategory = () => {
    const grouped: Record<string, Array<[string, ConfigValue]>> = {};

    Object.entries(config).forEach(([key, value]) => {
      const category = value.category || 'other';
      if (!grouped[category]) {
        grouped[category] = [];
      }
      grouped[category].push([key, value]);
    });

    return grouped;
  };

  const renderConfigValue = (key: string, configValue: ConfigValue) => {
    const currentValue = editedValues[key] !== undefined ? editedValues[key] : configValue.value;
    const isSensitive = configValue.sensitive;
    const isHidden = isSensitive && !showSensitive[key];

    if (configValue.type === 'boolean') {
      const isRestartRequired = configValue.requires_restart === true;
      return (
        <div className="flex items-center gap-2">
          <Switch
            checked={currentValue as boolean}
            onCheckedChange={(checked) => {
              if (!isRestartRequired) handleValueChange(key, checked);
            }}
            disabled={isRestartRequired}
            className={isRestartRequired ? 'cursor-not-allowed opacity-60' : ''}
          />
          <span className="text-sm">{currentValue ? 'Enabled' : 'Disabled'}</span>
          {isRestartRequired && (
            <span className="text-xs text-amber-600 dark:text-amber-400">
              (Restart required to change)
            </span>
          )}
        </div>
      );
    }

    if (isSensitive) {
      return (
        <div className="flex items-center gap-2">
          <Input
            type={isHidden ? 'password' : 'text'}
            value={isHidden ? '••••••••' : currentValue}
            onChange={(e) => handleValueChange(key, e.target.value)}
            className="flex-1 font-mono text-sm"
            readOnly={isHidden}
          />
          <Button
            variant="ghost"
            size="sm"
            onClick={() => toggleSensitive(key)}
          >
            {isHidden ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
          </Button>
        </div>
      );
    }

    if (configValue.type === 'enum' && configValue.enum_values) {
      // Read-only ENUM display with valid values list
      return (
        <div className="space-y-2">
          <div className="flex items-center gap-2 p-2 bg-gray-50 dark:bg-gray-800 rounded border dark:border-gray-700">
            <span className="font-mono text-sm font-medium">{currentValue}</span>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {configValue.enum_descriptions?.[currentValue as string] || ''}
            </span>
          </div>
          <div className="text-xs text-gray-600 dark:text-gray-400">
            <span className="font-medium">Valid values:</span>{' '}
            {configValue.enum_values.map((val, idx) => (
              <span key={val}>
                <code className="px-1 py-0.5 bg-gray-100 dark:bg-gray-800 rounded">{val}</code>
                {idx < configValue.enum_values!.length - 1 && ', '}
              </span>
            ))}
          </div>
        </div>
      );
    }

    if (configValue.type === 'number') {
      return (
        <div className="space-y-1">
          <Input
            type="number"
            step={currentValue.toString().includes('.') ? '0.01' : '1'}
            value={currentValue}
            onChange={(e) => handleValueChange(key, parseFloat(e.target.value))}
            className="font-mono text-sm"
            readOnly
          />
          {(configValue.min_value !== undefined || configValue.max_value !== undefined) && (
            <p className="text-xs text-gray-500">
              Range: {configValue.min_value ?? '−∞'} to {configValue.max_value ?? '+∞'}
            </p>
          )}
        </div>
      );
    }

    return (
      <Input
        type="text"
        value={currentValue}
        onChange={(e) => handleValueChange(key, e.target.value)}
        className="font-mono text-sm"
        readOnly
      />
    );
  };

  const getCategoryIcon = (category: string) => {
    const icons: Record<string, string> = {
      neural_memory: '🧠',
      embedding: '📊',
      search: '🔍',
      memory: '💾',
      system: '⚙️',
    };
    return icons[category] || '📁';
  };

  const getCategoryTitle = (category: string) => {
    const titles: Record<string, string> = {
      neural_memory: t('sections.neuralMemory'),
      embedding: t('sections.embedding'),
      search: t('sections.search'),
      memory: t('sections.memory'),
      system: t('sections.system'),
    };
    return titles[category] || category;
  };

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t('title')} description={t('loadingDesc')} />
        <SpinnerLoading size="lg" message={t('messages.loading')} />
      </PageContainer>
    );
  }

  if (error) {
    return (
      <PageContainer>
        <PageHeader title={t('title')} description={t('configManagement')} />
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </PageContainer>
    );
  }

  const groupedConfig = groupByCategory();
  const hasChanges = Object.keys(editedValues).length > 0;

  return (
    <PageContainer>
      <div className="flex items-start justify-between mb-6">
        <PageHeader
          title={t('titleFull')}
          description={t('description')}
        />

        <div className="flex gap-2">
          <Button onClick={loadConfig} variant="outline" disabled={loading}>
            {loading ? (
              <InlineSpinner size="sm" className="mr-2" />
            ) : (
              <RefreshCw className="h-4 w-4 mr-2" />
            )}
            {t('actions.refresh')}
          </Button>
          {hasChanges && (
            <Button onClick={handleSave} disabled={saving}>
              <Save className="h-4 w-4 mr-2" />
              {t('actions.saveChanges', { count: Object.keys(editedValues).length })}
            </Button>
          )}
        </div>
      </div>

      {hasChanges && (
        <Alert className="mb-6">
          <Settings className="h-4 w-4" />
          <AlertDescription>
            {t('messages.unsavedChanges', { count: Object.keys(editedValues).length })}
          </AlertDescription>
        </Alert>
      )}

      <div className="space-y-6">
        {/* System Status — Embedding & Services */}
        {telemetry && (
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Server className="h-5 w-5" />
                {t('systemStatus.title')}
              </CardTitle>
              <CardDescription>{t('systemStatus.description')}</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                {/* Embedding Config */}
                <div className="p-4 border dark:border-gray-700 rounded-lg space-y-2">
                  <div className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('systemStatus.embeddingConfig')}</div>
                  {telemetry.embedding_config ? (
                    <div className="space-y-1 text-sm">
                      <div className="flex justify-between">
                        <span className="text-gray-500">{t('systemStatus.provider')}</span>
                        <code className="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-xs">{telemetry.embedding_config.provider}</code>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-500">{t('systemStatus.model')}</span>
                        <code className="px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-xs">{telemetry.embedding_config.model}</code>
                      </div>
                      <div className="flex justify-between">
                        <span className="text-gray-500">{t('systemStatus.dimensions')}</span>
                        <span className="text-xs">{telemetry.embedding_config.dimensions}</span>
                      </div>
                    </div>
                  ) : (
                    <span className="text-xs text-gray-400">—</span>
                  )}
                </div>

                {/* Ollama Status */}
                <div className="p-4 border dark:border-gray-700 rounded-lg space-y-2">
                  <div className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('systemStatus.ollama')}</div>
                  {telemetry.services?.ollama ? (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        {telemetry.services.ollama.status === 'ok' ? (
                          <CheckCircle className="h-4 w-4 text-green-500" />
                        ) : telemetry.services.ollama.status === 'not_configured' ? (
                          <XCircle className="h-4 w-4 text-gray-400" />
                        ) : (
                          <XCircle className="h-4 w-4 text-red-500" />
                        )}
                        <span className="text-sm">{telemetry.services.ollama.status}</span>
                      </div>
                      {telemetry.services.ollama.details?.models && (
                        <div className="text-xs text-gray-500">
                          {(() => {
                            const embedModels = telemetry.services.ollama.details.models.filter((m: string) => m.includes('embed'));
                            return embedModels.length > 0
                              ? t('systemStatus.embeddingModels', { models: embedModels.join(', ') })
                              : t('systemStatus.modelsAvailable', { count: telemetry.services.ollama.details.models.length });
                          })()}
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="flex items-center gap-2">
                      <XCircle className="h-4 w-4 text-gray-400" />
                      <span className="text-sm text-gray-400">{t('systemStatus.ollamaNotConfigured')}</span>
                    </div>
                  )}
                </div>

                {/* Qdrant Collections */}
                <div className="p-4 border dark:border-gray-700 rounded-lg space-y-2">
                  <div className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('systemStatus.qdrantCollections')}</div>
                  {telemetry.services?.qdrant?.status === 'ok' ? (
                    telemetry.services.qdrant.details?.collection_names?.length > 0 ? (
                      <div className="space-y-1">
                        {telemetry.services.qdrant.details.collection_names.map((name: string) => (
                          <div key={name} className="flex items-center gap-2">
                            <div className="w-2 h-2 rounded-full bg-green-500" />
                            <code className="text-xs font-mono">{name}</code>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="flex items-center gap-2">
                        <CheckCircle className="h-4 w-4 text-green-500" />
                        <span className="text-sm text-gray-500">{t('systemStatus.qdrantNoCollections')}</span>
                      </div>
                    )
                  ) : (
                    <div className="flex items-center gap-2">
                      <XCircle className="h-4 w-4 text-red-500" />
                      <span className="text-sm text-gray-500">{telemetry.services?.qdrant?.details?.error || t('systemStatus.qdrantError')}</span>
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
                {t('messages.configParams', { count: items.length, s: items.length !== 1 ? 's' : '' })}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                {items.map(([key, configValue]) => (
                  <div key={key} className="space-y-3 p-4 border dark:border-gray-700 rounded-lg bg-white dark:bg-gray-900">
                    {/* Header with key name and badges */}
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <Label htmlFor={key} className="font-mono text-sm font-semibold">
                          {key}
                        </Label>
                        {configValue.requires_restart && (
                          <Badge variant="destructive" className="text-xs">
                            Requires Restart
                          </Badge>
                        )}
                        {configValue.type === 'enum' && (
                          <Badge variant="outline" className="text-xs">
                            ENUM
                          </Badge>
                        )}
                      </div>
                      {configValue.documentation_url && (
                        <a
                          href={configValue.documentation_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
                        >
                          <ExternalLink className="h-4 w-4" />
                        </a>
                      )}
                    </div>

                    {/* Description */}
                    {configValue.description && (
                      <p className="text-sm text-gray-600 dark:text-gray-400">{configValue.description}</p>
                    )}

                    {/* Value input */}
                    <div>{renderConfigValue(key, configValue)}</div>

                    {/* Extended metadata panel */}
                    {(configValue.impact || configValue.recommended || configValue.examples) && (
                      <div className="mt-3 p-3 bg-blue-50 dark:bg-blue-900/20 rounded-md space-y-2">
                        {configValue.impact && (
                          <div className="flex items-start gap-2">
                            <Info className="h-4 w-4 text-blue-600 dark:text-blue-400 mt-0.5 flex-shrink-0" />
                            <div>
                              <p className="text-xs font-medium text-blue-900 dark:text-blue-100">Impact</p>
                              <p className="text-xs text-blue-700 dark:text-blue-300">{configValue.impact}</p>
                            </div>
                          </div>
                        )}
                        {configValue.recommended && (
                          <div className="text-xs">
                            <span className="font-medium text-green-900 dark:text-green-100">Recommended:</span>{' '}
                            <span className="text-green-700 dark:text-green-300">{configValue.recommended}</span>
                          </div>
                        )}
                        {configValue.examples && configValue.examples.length > 0 && (
                          <div className="text-xs">
                            <span className="font-medium text-gray-700 dark:text-gray-300">Examples:</span>{' '}
                            {configValue.examples.map((ex, idx) => (
                              <span key={idx}>
                                <code className="px-1 py-0.5 bg-white dark:bg-gray-800 rounded text-gray-800 dark:text-gray-200">{ex}</code>
                                {idx < configValue.examples!.length - 1 && ', '}
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

      {/* Floating save bar — visible when scrolled and changes exist */}
      {hasChanges && (
        <div className="fixed bottom-0 left-0 right-0 z-50 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60 px-6 py-3">
          <div className="container mx-auto flex items-center justify-between">
            <span className="text-sm text-muted-foreground">
              {t('actions.saveChanges', { count: Object.keys(editedValues).length })}
            </span>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setEditedValues({})}>
                {t('actions.discard', { default: 'Discard' })}
              </Button>
              <Button size="sm" onClick={handleSave} disabled={saving}>
                <Save className="h-4 w-4 mr-1" />
                {t('actions.save', { default: 'Save' })}
              </Button>
            </div>
          </div>
        </div>
      )}
    </PageContainer>
  );
}
