'use client';

/**
 * External API Keys Management Page
 *
 * Manage user-specific external API keys (OpenAI, Cohere, Anthropic, etc.)
 * Issue #45 - External Keys UI for per-user configuration
 * Issue #115 - Added OpenAI required alert
 * Issue #223 - i18n support
 * Fix: Added permission check and redirect on workspace change
 */

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { PageHeader } from '@/components/common/PageHeader';
import { PageContainer } from '@/components/common/PageContainer';
import { FeatureGuide } from '@/components/common/FeatureGuide';
import { LoadingState } from '@/components/common/LoadingState';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Key, Plus, Trash2, Edit, RefreshCw, AlertCircle, CheckCircle, AlertTriangle, ChevronDown } from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Switch } from '@/components/ui/switch';
import {
  listExternalAPIKeys,
  createExternalAPIKey,
  updateExternalAPIKey,
  deleteExternalAPIKey,
  toggleExternalAPIKey,
  type ExternalAPIKey,
} from '@/lib/api/external-keys';
import { useToast } from '@/hooks/use-toast';
import { InlineSpinner } from '@/components/common/LoadingState';
import { useMemoryContext } from '@/contexts/MemoryContextContext';
import { useWorkspace } from '@/contexts/WorkspaceContext';

export default function ExternalKeysPage() {
  const t = useTranslations('externalKeys');
  const tCommon = useTranslations('common');
  const router = useRouter();

  // Provider definitions with auto-generated key names
  const PROVIDERS = [
    { value: 'openai', label: t('providers.openai'), keyName: 'OPENAI_API_KEY', icon: '🔑', description: t('providers.openaiDesc') },
    { value: 'voyage', label: t('providers.voyage'), keyName: 'VOYAGE_API_KEY', icon: '🚀', description: t('providers.voyageDesc') },
    { value: 'cohere', label: t('providers.cohere'), keyName: 'COHERE_API_KEY', icon: '🧬', description: t('providers.cohereDesc') },
  ];

  const { contextId } = useMemoryContext();  // For context-scoped keys
  const { currentWorkspaceId, currentWorkspace } = useWorkspace();  // For workspace-scoped keys
  const [keys, setKeys] = useState<ExternalAPIKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteKeyName, setDeleteKeyName] = useState<string | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<ExternalAPIKey | null>(null);
  const [formData, setFormData] = useState({ key_name: '', provider: '', value: '' });
  const { toast } = useToast();

  // Filter providers - only show ones not already configured
  const availableProviders = PROVIDERS.filter(
    (p) => !keys.some((k) => k.provider === p.value)
  );

  // Issue #169: Quick add handler for dropdown
  const handleQuickAdd = (provider: typeof PROVIDERS[0]) => {
    setFormData({
      key_name: provider.keyName,
      provider: provider.value,
      value: '',
    });
    setCreateDialogOpen(true);
  };

  // Issue #115: Check if OpenAI key is configured (required for memory operations)
  const hasOpenAIKey = keys.some((k) => k.provider === 'openai' && k.enabled);

  const handleProviderChange = (provider: string) => {
    const selectedProvider = PROVIDERS.find((p) => p.value === provider);
    setFormData({
      ...formData,
      provider,
      key_name: selectedProvider?.keyName || '',
    });
  };

  // Permission check: Redirect if not owner when workspace changes
  useEffect(() => {
    if (currentWorkspace && currentWorkspace.current_user_role !== 'owner') {
      router.push('/workspace/dashboard');
    }
  }, [currentWorkspace, router]);

  useEffect(() => {
    // Re-fetch when context or workspace changes
    // Workspace-scoped keys are shown for all contexts in the workspace
    // Context-scoped keys are shown for specific context only
    // Issue #213: contextId can be null (user may not have context yet)
    if (currentWorkspaceId !== null) {
      loadKeys();
    }
  }, [contextId, currentWorkspaceId]);

  const loadKeys = async () => {
    try {
      setLoading(true);
      const response = await listExternalAPIKeys();
      // API returns { keys: [], total: 0 } structure
      setKeys(Array.isArray(response) ? response : (response as any).keys || []);
    } catch (error) {
      toast({
        title: tCommon('error'),
        description: t('errorLoadKeys'),
        variant: 'destructive',
      });
    } finally {
      setLoading(false);
    }
  };

  const handleCreate = async () => {
    try {
      await createExternalAPIKey(formData);
      toast({
        title: tCommon('success'),
        description: t('successCreate'),
      });
      setCreateDialogOpen(false);
      setFormData({ key_name: '', provider: '', value: '' });
      loadKeys();
    } catch (error: any) {
      const errorMessage = error?.details?.detail || error?.message || t('errorCreateKey');
      toast({
        title: tCommon('error'),
        description: errorMessage,
        variant: 'destructive',
      });
    }
  };

  const handleUpdate = async () => {
    if (!selectedKey) return;

    try {
      await updateExternalAPIKey(selectedKey.key_name, formData.value);
      toast({
        title: tCommon('success'),
        description: t('successUpdate'),
      });
      setEditDialogOpen(false);
      setSelectedKey(null);
      setFormData({ key_name: '', provider: '', value: '' });
      loadKeys();
    } catch (error: any) {
      const errorMessage = error?.details?.detail || error?.message || t('errorUpdateKey');
      toast({
        title: tCommon('error'),
        description: errorMessage,
        variant: 'destructive',
      });
    }
  };

  const openDeleteDialog = (keyName: string) => {
    setDeleteKeyName(keyName);
    setDeleteError(null);
    setDeleteDialogOpen(true);
  };

  const handleDelete = async () => {
    if (!deleteKeyName) return;

    try {
      setDeleteLoading(true);
      setDeleteError(null);
      await deleteExternalAPIKey(deleteKeyName);
      toast({
        title: tCommon('success'),
        description: t('successDelete'),
      });
      setDeleteDialogOpen(false);
      setDeleteKeyName(null);
      loadKeys();
    } catch (error: any) {
      const errorMessage = error?.details?.detail || error?.message || t('errorDeleteKey');
      setDeleteError(errorMessage);
      setDeleteLoading(false);
    }
  };

  const handleToggle = async (key: ExternalAPIKey) => {
    try {
      await toggleExternalAPIKey(key.key_name, !key.enabled);
      const action = key.enabled ? t('disabled') : t('enabled');
      toast({
        title: tCommon('success'),
        description: t('successToggle', { action }),
      });
      loadKeys();
    } catch (error: any) {
      const detail = error?.details?.detail;
      if (detail?.error === 'reranker_provider_conflict') {
        toast({
          title: t('conflict'),
          description: detail.message,
          variant: 'destructive',
        });
      } else if (detail?.error === 'cannot_disable_embeddings') {
        toast({
          title: t('cannotDisable'),
          description: detail.message,
          variant: 'destructive',
        });
      } else {
        toast({
          title: tCommon('error'),
          description: t('errorToggleKey'),
          variant: 'destructive',
        });
      }
    }
  };

  const openEditDialog = (key: ExternalAPIKey) => {
    setSelectedKey(key);
    setFormData({ key_name: key.key_name, provider: key.provider, value: '' });
    setEditDialogOpen(true);
  };

  if (loading && keys.length === 0) {
    return (
      <PageContainer>
        <PageHeader
          title={t('title')}
          description={t('description')}
        />
        <LoadingState lines={3} />
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
            <Button onClick={loadKeys} variant="outline" disabled={loading}>
              {loading ? <InlineSpinner size="sm" className="mr-2" /> : <RefreshCw className="h-4 w-4 mr-2" />}
              {t('refresh')}
            </Button>
            {/* Issue #169: Dropdown for API Key creation */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  size="lg"
                  className="bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white shadow-lg hover:from-brand-green-700 hover:to-emerald-700"
                >
                  <Plus className="mr-2 h-5 w-5" />
                  {t('addApiKey')}
                  <ChevronDown className="ml-2 h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-64">
                {availableProviders.length === 0 ? (
                  <div className="px-2 py-3 text-sm text-muted-foreground text-center">
                    {t('allProvidersConfigured')}
                  </div>
                ) : (
                  availableProviders.map((provider) => (
                    <DropdownMenuItem
                      key={provider.value}
                      onClick={() => handleQuickAdd(provider)}
                    >
                      <span className="mr-3 text-lg">{provider.icon}</span>
                      <div>
                        <div className="font-medium">{provider.label}</div>
                        <div className="text-xs text-muted-foreground">{provider.description}</div>
                      </div>
                    </DropdownMenuItem>
                  ))
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        }
      />

      <FeatureGuide storageKey="external-keys" title={t('featureGuide.title')}>
        <p>{t('featureGuide.overview')}</p>
        <p>{t('featureGuide.useCases')}</p>
        <p className="font-medium">{t('featureGuide.howItWorks')}</p>
      </FeatureGuide>

      {/* Issue #115: OpenAI Required Alert */}
      {!loading && !hasOpenAIKey && (
        <Alert variant="destructive" className="mb-6 border-red-300 bg-red-50 dark:bg-red-950/50">
          <AlertTriangle className="h-5 w-5" />
          <AlertDescription className="ml-2">
            <strong className="font-semibold">{t('openAIRequired')}</strong>
            <p className="mt-1 text-sm">
              {t('openAIRequiredDesc')}
            </p>
          </AlertDescription>
        </Alert>
      )}

      {/* Success indicator when OpenAI is configured */}
      {!loading && hasOpenAIKey && (
        <Alert className="mb-6 border-green-300 bg-green-50 dark:bg-green-950/50">
          <CheckCircle className="h-5 w-5 text-green-600" />
          <AlertDescription className="ml-2 text-green-800 dark:text-green-200">
            <strong className="font-semibold">{t('openAIConfigured')}</strong> - {t('openAIConfiguredDesc')}
          </AlertDescription>
        </Alert>
      )}

      <Alert className="mb-6">
        <AlertCircle className="h-4 w-4" />
        <AlertDescription>
          {t('securityNotice')}
        </AlertDescription>
      </Alert>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Key className="h-5 w-5" />
            {t('yourApiKeys')}
          </CardTitle>
          <CardDescription>
            {keys.length === 1 ? t('keysConfigured', { count: keys.length }) : t('keysConfiguredPlural', { count: keys.length })}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p className="text-center py-8 text-gray-500">{t('loading')}</p>
          ) : keys.length === 0 ? (
            <div className="text-center py-12">
              <Key className="h-12 w-12 text-gray-400 mx-auto mb-4" />
              <p className="text-gray-600 mb-4">{t('noKeysYet')}</p>
              <Button onClick={() => setCreateDialogOpen(true)}>
                <Plus className="h-4 w-4 mr-2" />
                {t('addFirstKey')}
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t('provider')}</TableHead>
                  <TableHead>{t('keyName')}</TableHead>
                  <TableHead>{t('value')}</TableHead>
                  <TableHead>{t('updated')}</TableHead>
                  <TableHead className="text-right">{t('actions')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.map((key) => (
                  <TableRow key={key.id} className={!key.enabled ? 'opacity-50' : ''}>
                    <TableCell className="font-medium">
                      {key.provider}
                      {key.enabled && ['cohere', 'voyage'].includes(key.provider) && (
                        <span className="ml-2 text-xs text-orange-600">{t('activeReranker')}</span>
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-sm">{key.key_name}</TableCell>
                    <TableCell className="font-mono text-xs text-gray-500">{key.masked_value}</TableCell>
                    <TableCell className="text-sm text-gray-500">
                      {new Date(key.updated_at).toLocaleDateString()}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-2">
                        <Switch
                          checked={key.enabled}
                          onCheckedChange={() => handleToggle(key)}
                          disabled={key.provider === 'openai'}
                          title={
                            key.provider === 'openai'
                              ? t('openAICannotDisable')
                              : t('toggleEnabled')
                          }
                        />
                        <Button variant="ghost" size="sm" onClick={() => openEditDialog(key)}>
                          <Edit className="h-4 w-4" />
                        </Button>
                        {key.key_name !== 'OPENAI_API_KEY' ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => openDeleteDialog(key.key_name)}
                          >
                            <Trash2 className="h-4 w-4 text-red-600" />
                          </Button>
                        ) : (
                          <div className="text-xs text-muted-foreground px-2">
                            {t('required')}
                          </div>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Create Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('addExternalKey')}</DialogTitle>
            <DialogDescription>
              {t('addNewKey')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="provider">{t('provider')}</Label>
              <Select value={formData.provider} onValueChange={handleProviderChange}>
                <SelectTrigger>
                  <SelectValue placeholder={t('selectProvider')} />
                </SelectTrigger>
                <SelectContent>
                  {availableProviders.length === 0 ? (
                    <div className="px-2 py-1.5 text-sm text-gray-500">
                      {t('allProvidersConfigured')}
                    </div>
                  ) : (
                    availableProviders.map((provider) => (
                      <SelectItem key={provider.value} value={provider.value}>
                        {provider.label}
                      </SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label htmlFor="key_name">{t('keyNameAutoGenerated')}</Label>
              <Input
                id="key_name"
                value={formData.key_name}
                readOnly
                className="bg-gray-50 dark:bg-slate-800 dark:text-slate-200"
              />
            </div>
            <div>
              <Label htmlFor="value">{t('apiKeyValue')}</Label>
              <Input
                id="value"
                type="password"
                placeholder="sk-..."
                value={formData.value}
                onChange={(e) => setFormData({ ...formData, value: e.target.value })}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>
              {tCommon('cancel')}
            </Button>
            <Button onClick={handleCreate}>{tCommon('create')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('updateApiKey')}</DialogTitle>
            <DialogDescription>
              {t('updateKeyValue', { keyName: selectedKey?.key_name || '' })}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="edit_value">{t('newApiKeyValue')}</Label>
              <Input
                id="edit_value"
                type="password"
                placeholder={t('enterNewValue')}
                value={formData.value}
                onChange={(e) => setFormData({ ...formData, value: e.target.value })}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              {tCommon('cancel')}
            </Button>
            <Button onClick={handleUpdate}>{tCommon('update')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={(open) => {
        if (!deleteLoading) {
          setDeleteDialogOpen(open);
          if (!open) {
            setDeleteKeyName(null);
            setDeleteError(null);
          }
        }
      }}>
        <DialogContent className="sm:max-w-[500px]">
          <DialogHeader>
            <DialogTitle>{t('deleteApiKey')}</DialogTitle>
            <DialogDescription>
              {t('deleteKeyPermanent')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertDescription>
                {t('deleteWarning')}
              </AlertDescription>
            </Alert>
            {deleteError && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{deleteError}</AlertDescription>
              </Alert>
            )}
            <p className="text-sm">
              {t('deleteConfirm', { keyName: deleteKeyName || '' })}
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)} disabled={deleteLoading}>
              {tCommon('cancel')}
            </Button>
            <Button variant="destructive" onClick={handleDelete} disabled={deleteLoading}>
              {deleteLoading ? t('deleting') : tCommon('delete')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageContainer>
  );
}
