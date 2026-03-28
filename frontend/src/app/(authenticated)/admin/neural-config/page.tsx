'use client';

/**
 * Admin Neural Config Management Page
 *
 * Manage Neural Memory system configuration.
 * Admin-only page (Issue #107).
 */

import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { PageHeader } from '@/components/common/PageHeader';
import { PageContainer } from '@/components/common/PageContainer';
import { Section } from '@/components/common/Section';
import { LoadingState } from '@/components/common/LoadingState';
import { apiClient } from '@/lib/api';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { RefreshCw, Save, RotateCcw, Check, X, Pencil } from 'lucide-react';
import { useToast } from '@/hooks/use-toast';
import { InlineSpinner } from '@/components/common/LoadingState';

interface NeuralConfigItem {
  key: string;
  value: string;
  value_type: string;
  category: string;
  description: string | null;
  min_value: number | null;
  max_value: number | null;
  updated_at: string;
}

interface EditingState {
  key: string;
  value: string;
}

export default function AdminNeuralConfigPage() {
  const t = useTranslations('admin.neuralConfig');
  const tCommon = useTranslations('admin.common');

  const [configs, setConfigs] = useState<NeuralConfigItem[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [editing, setEditing] = useState<EditingState | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const { toast } = useToast();

  useEffect(() => {
    loadConfigs();
  }, []);

  const loadConfigs = async () => {
    try {
      setLoading(true);
      const data = await apiClient.get<{
        configs: NeuralConfigItem[];
        categories: string[];
        total: number;
      }>('/api/v1/admin/neural-config');
      setConfigs(data.configs);
      setCategories(data.categories);
    } catch (error) {
      toast({
        title: tCommon('error'),
        description: t('messages.loadError'),
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleEdit = (config: NeuralConfigItem) => {
    setEditing({ key: config.key, value: config.value });
  };

  const handleCancelEdit = () => {
    setEditing(null);
  };

  const handleSave = async (key: string, newValue: string) => {
    try {
      setSaving(key);
      await apiClient.put(`/api/v1/admin/neural-config/${key}`, { value: newValue });
      toast({
        title: tCommon('success'),
        description: t('messages.updateSuccess', { key }),
      });
      setEditing(null);
      loadConfigs();
    } catch (error: any) {
      toast({
        title: tCommon('error'),
        description: error?.details?.detail || t('messages.updateError'),
        variant: 'destructive',
      });
    } finally {
      setSaving(null);
    }
  };

  const handleReset = async () => {
    if (!confirm(t('messages.resetConfirm'))) {
      return;
    }

    try {
      setResetting(true);
      const result = await apiClient.post<{ message: string; reset_count: number }>(
        '/api/v1/admin/neural-config/reset'
      );
      toast({
        title: tCommon('success'),
        description: result.message,
      });
      loadConfigs();
    } catch (error) {
      toast({
        title: tCommon('error'),
        description: t('messages.resetError'),
        variant: 'destructive',
      });
    } finally {
      setResetting(false);
    }
  };

  const getCategoryColor = (category: string) => {
    const colors: Record<string, string> = {
      hebbian: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
      spreading: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
      scoring: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
      temporal: 'bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300',
      decay: 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300',
      coactivation: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
      consolidation: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/30 dark:text-indigo-300',
      performance: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300',
    };
    return colors[category] || 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300';
  };

  const filteredConfigs = selectedCategory
    ? configs.filter((c) => c.category === selectedCategory)
    : configs;

  if (loading) {
    return (
      <PageContainer>
        <PageHeader
          title={t('title')}
          description={t('description')}
        />
        <div className="text-center py-8">
          <p className="text-gray-500">{t('messages.loading')}</p>
          <LoadingState lines={5} />
        </div>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={t('title')}
        description={t('description')}
        actions={
          <div className="flex gap-2">
            <Button onClick={handleReset} variant="outline" disabled={resetting}>
              {resetting ? (
                <InlineSpinner size="sm" className="mr-2" />
              ) : (
                <RotateCcw className="h-4 w-4 mr-2" />
              )}
              {t('actions.resetToDefaults')}
            </Button>
            <Button onClick={loadConfigs} variant="outline" disabled={loading}>
              {loading ? (
                <InlineSpinner size="sm" className="mr-2" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-2" />
              )}
              {t('actions.refresh')}
            </Button>
          </div>
        }
      />

      {/* Category Filter */}
      <Section title={t('filter.title')}>
        <div className="flex flex-wrap gap-2 mb-4">
          <Button
            variant={selectedCategory === null ? 'default' : 'outline'}
            size="sm"
            onClick={() => setSelectedCategory(null)}
          >
            {t('filter.all', { count: configs.length })}
          </Button>
          {categories.map((cat) => (
            <Button
              key={cat}
              variant={selectedCategory === cat ? 'default' : 'outline'}
              size="sm"
              onClick={() => setSelectedCategory(cat)}
            >
              {t('filter.category', { category: cat, count: configs.filter((c) => c.category === cat).length })}
            </Button>
          ))}
        </div>
      </Section>

      {/* Config Table */}
      <Section title={t('table.title')}>
        <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('table.key')}</TableHead>
                <TableHead>{t('table.category')}</TableHead>
                <TableHead>{t('table.value')}</TableHead>
                <TableHead>{t('table.range')}</TableHead>
                <TableHead>{t('table.description')}</TableHead>
                <TableHead className="text-right">{t('table.actions')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredConfigs.map((config) => (
                <TableRow key={config.key}>
                  <TableCell className="font-mono text-sm">{config.key}</TableCell>
                  <TableCell>
                    <Badge className={getCategoryColor(config.category)}>
                      {config.category}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {editing?.key === config.key ? (
                      <Input
                        value={editing.value}
                        onChange={(e) =>
                          setEditing({ ...editing, value: e.target.value })
                        }
                        className="w-24 h-8 text-sm"
                        type={config.value_type === 'int' ? 'number' : 'text'}
                        step={config.value_type === 'float' ? '0.01' : '1'}
                      />
                    ) : (
                      <span className="font-mono text-sm">{config.value}</span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-gray-500 dark:text-gray-400">
                    {config.min_value !== null && config.max_value !== null
                      ? `${config.min_value} - ${config.max_value}`
                      : '-'}
                  </TableCell>
                  <TableCell className="text-sm text-gray-600 dark:text-gray-400 max-w-md">
                    <div className="whitespace-normal break-words">
                      {config.description || '-'}
                    </div>
                  </TableCell>
                  <TableCell className="text-right">
                    {editing?.key === config.key ? (
                      <div className="flex justify-end gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => handleSave(config.key, editing.value)}
                          disabled={saving === config.key}
                          title="Save changes"
                        >
                          {saving === config.key ? (
                            <InlineSpinner size="sm" />
                          ) : (
                            <Check className="h-4 w-4 text-green-600" />
                          )}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={handleCancelEdit}
                          title="Cancel editing"
                        >
                          <X className="h-4 w-4 text-red-600" />
                        </Button>
                      </div>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => handleEdit(config)}
                        title="Edit this value"
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Section>
    </PageContainer>
  );
}
