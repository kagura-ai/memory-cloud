'use client';

/**
 * Contexts Management Page
 *
 * Allows users to create, view, switch, and delete contexts.
 * Each context corresponds to a separate Qdrant collection for memory isolation.
 *
 * Issue #82: Context-based Multi-Collection Support
 * Issue #223: i18n support
 */

import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import Link from 'next/link';
import { useTranslations, useLocale } from 'next-intl';
import { useAuth } from '@/contexts/AuthContext';
import { useWorkspace } from '@/contexts/WorkspaceContext';
import {
  Plus,
  RefreshCw,
  Trash2,
  Edit,
  FolderOpen,
  Brain,
  Check,
  AlertCircle,
  AlertTriangle,
  Database,
  Loader2,
  Zap,
  Settings2,
  ChevronDown,
  Shield,
  BarChart,
  Network,
  Users,
  Copy,
  MoreVertical,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { PageHeader } from '@/components/common/PageHeader';
import { PageContainer } from '@/components/common/PageContainer';
import { Section } from '@/components/common/Section';
import { SpinnerLoading } from '@/components/common/LoadingState';
import { cn, typography, colors, transitions } from '@/styles/design-tokens';
import {
  getContexts,
  createContext,
  updateContext,
  deleteContext,
  getContextStats,
  getContextSearchConfig,
  updateContextSearchConfig,
  listContextMembers,
  type ContextMember,
} from '@/lib/api/contexts';
import { checkOpenAIKeyStatus } from '@/lib/api/workspaces';
import { getResourceImpact } from '@/lib/api/schemas';
import type { Context, ContextStats } from '@/lib/types/context';
import { CONTEXT_TEMPLATES, getTemplate } from '@/lib/templates/usage-guide';
import { createExternalAPIKey } from '@/lib/api/external-keys';
import { useToast } from '@/hooks/use-toast';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

// Constants (must match backend validation)
const CONTEXT_NAME_PATTERN = /^[a-z0-9_-]+$/;

export default function ContextsPage() {
  const t = useTranslations('contexts');
  const tCommon = useTranslations('common');
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, refetchUser } = useAuth();
  const { currentWorkspace } = useWorkspace();
  const [contexts, setContexts] = useState<Context[]>([]);
  const [loading, setLoading] = useState(true);

  // Check if context quota is reached (Issue #188)
  const isQuotaReached =
    currentWorkspace?.plan_name === 'free' && contexts.length >= 1;
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [hasOpenAIKey, setHasOpenAIKey] = useState<boolean | null>(null);  // Issue #165: API key check

  // Create dialog state (Advanced)
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [newContextName, setNewContextName] = useState('');
  const [newContextDisplayName, setNewContextDisplayName] = useState('');
  const [newContextDescription, setNewContextDescription] = useState('');
  const [newContextSummary, setNewContextSummary] = useState('');
  const [newContextUsageGuide, setNewContextUsageGuide] = useState('');
  // Note: Embedding model is now fixed via EMBEDDING_MODEL env var (single collection mode)
  const [isPrivate, setIsPrivate] = useState(true);  // Issue #165: Privacy control
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // Quick Create dialog state (Issue #169)
  const [quickCreateDialogOpen, setQuickCreateDialogOpen] = useState(false);
  const [quickCreateName, setQuickCreateName] = useState('');
  const [quickCreateError, setQuickCreateError] = useState<string | null>(null);
  const [quickCreating, setQuickCreating] = useState(false);

  // Delete dialog state
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [contextToDelete, setContextToDelete] = useState<Context | null>(null);

  // Unpublish confirmation dialog (Issue #238, #41)
  const [unpublishDialogOpen, setUnpublishDialogOpen] = useState(false);
  const [unpublishTokenCount, setUnpublishTokenCount] = useState(0);

  // Template selector toggle
  const [showTemplate, setShowTemplate] = useState(false);

  // Context Members Dialog
  const [membersDialogOpen, setMembersDialogOpen] = useState(false);
  const [selectedContext, setSelectedContext] = useState<Context | null>(null);
  const [contextMembers, setContextMembers] = useState<ContextMember[]>([]);
  const [membersLoading, setMembersLoading] = useState(false);

  // Resource ID copy state
  const [copiedResourceId, setCopiedResourceId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  // Edit dialog state
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [contextToEdit, setContextToEdit] = useState<Context | null>(null);
  const [editDisplayName, setEditDisplayName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editSummary, setEditSummary] = useState('');
  const [editUsageGuide, setEditUsageGuide] = useState('');
  const [editIsPrivate, setEditIsPrivate] = useState(true);
  const [editIsPublic, setEditIsPublic] = useState(false);  // Issue #238
  const [editResourceIdPrefix, setEditResourceIdPrefix] = useState('');  // Issue #238
  const [editing, setEditing] = useState(false);

  // API Key setup dialog state
  const [apiKeyDialogOpen, setApiKeyDialogOpen] = useState(false);
  const [apiKeyValue, setApiKeyValue] = useState('');
  const [apiKeySaving, setApiKeySaving] = useState(false);
  const [apiKeyError, setApiKeyError] = useState<string | null>(null);
  const { toast } = useToast();

  // Quota limit dialog state
  const [quotaDialogOpen, setQuotaDialogOpen] = useState(false);

  // Stats state
  const [contextStats, setContextStats] = useState<Record<string, ContextStats>>({});
  const [loadingStats, setLoadingStats] = useState<Record<string, boolean>>({});


  const fetchContexts = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await getContexts();
      setContexts(response.contexts);
    } catch (err) {
      console.error('Failed to fetch contexts:', err);
      setError('Failed to load contexts');
    } finally {
      setLoading(false);
    }
  }, []);

  const checkApiKey = useCallback(async () => {
    if (!user?.current_workspace_id) return;

    try {
      const status = await checkOpenAIKeyStatus(user.current_workspace_id);
      setHasOpenAIKey(status.has_key);
    } catch (err) {
      console.error('Failed to check API key status:', err);
      setHasOpenAIKey(null);
    }
  }, [user?.current_workspace_id]);

  useEffect(() => {
    fetchContexts();
    checkApiKey();
  }, [fetchContexts, checkApiKey]);

  // Open edit modal from URL param: /workspace/contexts?edit=<context_id>
  const editHandled = useRef(false);
  useEffect(() => {
    const editId = searchParams.get('edit');
    if (editId && contexts.length > 0 && !editHandled.current) {
      const ctx = contexts.find(c => c.id === editId);
      if (ctx) {
        editHandled.current = true;
        handleEditClick(ctx);
        router.replace('/workspace/contexts', { scroll: false });
      }
    }
  }, [searchParams, contexts]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-select Shared for Admins when opening create dialogs
  useEffect(() => {
    if ((createDialogOpen || quickCreateDialogOpen) && currentWorkspace?.current_user_role === 'admin') {
      setIsPrivate(false);  // Admins can only create shared contexts
    }
  }, [createDialogOpen, quickCreateDialogOpen, currentWorkspace?.current_user_role]);

  const handleRefresh = () => {
    fetchContexts();
  };

  // Issue #169: Quick Create - minimal form, just name
  const handleQuickCreate = async () => {
    if (!quickCreateName.trim()) {
      setQuickCreateError(t('nameRequired'));
      return;
    }

    if (!CONTEXT_NAME_PATTERN.test(quickCreateName)) {
      setQuickCreateError(t('invalidName'));
      return;
    }

    try {
      setQuickCreating(true);
      setQuickCreateError(null);
      await createContext({
        name: quickCreateName.trim(),
        is_private: isPrivate,  // Issue #182: Privacy control
      });
      setQuickCreateDialogOpen(false);
      setQuickCreateName('');
      setIsPrivate(true);  // Reset to default
      await refetchUser();
      fetchContexts();
    } catch (err: unknown) {
      console.error('Failed to create context:', err);
      const apiError = err as {
        message?: string;
        details?: { detail?: string };
        response?: { data?: { detail?: string } };
      };

      let errorMessage =
        apiError?.response?.data?.detail ||
        apiError?.details?.detail ||
        apiError?.message ||
        t('failedToCreate');

      // Translate common error messages
      if (errorMessage.includes('Context limit reached')) {
        const planMatch = errorMessage.match(/Your (\w+) plan/i);
        const limitMatch = errorMessage.match(/allows (\d+) context/i);
        const plan = planMatch ? planMatch[1] : 'Free';
        const limit = limitMatch ? limitMatch[1] : '1';
        errorMessage = t('contextLimitReached', { plan, limit });
      } else if (errorMessage.includes('already exists') || errorMessage.includes('name taken')) {
        errorMessage = t('nameTaken');
      } else if (errorMessage.includes('Invalid') && errorMessage.includes('name')) {
        errorMessage = t('invalidName');
      }

      setQuickCreateError(errorMessage);
    } finally {
      setQuickCreating(false);
    }
  };

  // Advanced Create - full form with all options
  const handleCreateContext = async () => {
    if (!newContextName.trim()) {
      setCreateError(t('nameRequired'));
      return;
    }

    // Validate context name format (must match backend)
    if (!CONTEXT_NAME_PATTERN.test(newContextName)) {
      setCreateError(t('invalidName'));
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
        is_private: isPrivate,  // Issue #165
      });
      setCreateDialogOpen(false);
      setNewContextName('');
      setNewContextDisplayName('');
      setNewContextDescription('');
      setNewContextSummary('');
      setNewContextUsageGuide('');
      setIsPrivate(true);
      // Refresh user data to get updated current_context_id
      await refetchUser();
      fetchContexts();
    } catch (err: any) {
      console.error('Failed to create context:', err);

      // apiClient now extracts detail from FastAPI errors into message
      let errorMessage = err?.message || (typeof err === 'string' ? err : t('failedToCreate'));

      // Translate common error messages (but keep resource_id duplicates as-is)
      if (errorMessage.includes('already used')) {
        // Resource ID duplicate error - show API message as-is (includes context name)
        setCreateError(errorMessage);
      } else if (errorMessage.includes('Context limit reached')) {
        const planMatch = errorMessage.match(/Your (\w+) plan/i);
        const limitMatch = errorMessage.match(/allows (\d+) context/i);
        const plan = planMatch ? planMatch[1] : 'Free';
        const limit = limitMatch ? limitMatch[1] : '1';
        setCreateError(t('contextLimitReached', { plan, limit }));
      } else if (errorMessage.includes('already exists') || errorMessage.includes('name taken')) {
        setCreateError(t('nameTaken'));
      } else if (errorMessage.includes('Invalid') && errorMessage.includes('name')) {
        setCreateError(t('invalidName'));
      } else {
        setCreateError(errorMessage);
      }
    } finally {
      setCreating(false);
    }
  };

  const handleViewStats = async (context: Context) => {
    router.push(`/workspace/contexts/${context.id}/stats`);
  };

  const handleViewGraph = async (context: Context) => {
    router.push(`/workspace/contexts/${context.id}/graph`);
  };

  const handleDeleteClick = (context: Context) => {
    setContextToDelete(context);
    setDeleteDialogOpen(true);
  };

  const handleConfirmDelete = async () => {
    if (!contextToDelete) return;

    try {
      setDeleting(true);
      await deleteContext(contextToDelete.id);
      setDeleteDialogOpen(false);
      setContextToDelete(null);
      fetchContexts();
    } catch (err: unknown) {
      console.error('Failed to delete context:', err);
      const apiError = err as { details?: { detail?: string }; message?: string };
      const errorMessage = apiError?.details?.detail || apiError?.message || t('failedToDelete');
      setError(errorMessage);
    } finally {
      setDeleting(false);
    }
  };

  const handleEditClick = (context: Context) => {
    setContextToEdit(context);
    setEditDisplayName(context.display_name || context.name || '');
    setEditDescription(context.description || '');
    setEditSummary(context.summary || '');
    setEditUsageGuide(context.usage_guide || '');
    setEditIsPrivate(context.is_private);
    setEditIsPublic(context.is_public);  // Issue #238
    setEditResourceIdPrefix('');  // Reset prefix for new public contexts
    setEditDialogOpen(true);
  };

  const handleSaveEdit = async () => {
    if (!contextToEdit) return;

    try {
      setEditing(true);

      // Validate prefix if making public
      if (editIsPublic && !contextToEdit.is_public && !editResourceIdPrefix.trim()) {
        toast({
          title: t('resourceIdPrefixRequired'),
          description: t('resourceIdPrefixRequiredDesc'),
          variant: 'destructive',
        });
        setEditing(false);
        return;
      }

      // Generate resource_id if making public
      let resource_id: string | undefined = undefined;
      if (editIsPublic && !contextToEdit.is_public) {
        const prefix = editResourceIdPrefix.trim();
        resource_id = prefix;  // Use prefix directly (no UUID)
      }

      await updateContext(contextToEdit.id, {
        display_name: editDisplayName.trim() || undefined,
        description: editDescription.trim() || undefined,
        summary: editSummary.trim() || undefined,
        usage_guide: editUsageGuide.trim() || undefined,
        is_private: editIsPrivate,
        is_public: editIsPublic,  // Issue #238
        resource_id: resource_id,  // Issue #238
      });

      // Success: close dialog and show toast
      setEditDialogOpen(false);
      setContextToEdit(null);
      toast({
        title: t('contextUpdated'),
        description: t('contextUpdatedDesc'),
      });
      fetchContexts();
    } catch (err: any) {
      // apiClient now extracts detail from FastAPI errors into message
      const errorMsg = err?.message || (typeof err === 'string' ? err : t('failedToUpdate'));

      console.error('Failed to update context:', errorMsg, 'Full error:', err);

      // Translate common error messages
      let userMessage = errorMsg;
      if (errorMsg.includes('private context') || errorMsg.includes('creator only')) {
        userMessage = t('privateContextEditError');
      } else if (errorMsg.includes('owner')) {
        userMessage = t('ownerOnlyEditError');
      } else if (errorMsg.includes('already used')) {
        // Resource ID duplicate error - show API message as-is (includes context name)
        userMessage = errorMsg;
      }

      toast({
        title: t('updateFailed'),
        description: userMessage,
        variant: 'destructive',
        duration: 6000,  // Longer duration for detailed error messages
      });
    } finally {
      setEditing(false);
    }
  };

  const handleLoadStats = async (context: Context) => {
    if (loadingStats[context.id]) return;

    try {
      setLoadingStats((prev) => ({ ...prev, [context.id]: true }));
      const stats = await getContextStats(context.id);
      setContextStats((prev) => ({ ...prev, [context.id]: stats }));
    } catch (err) {
      console.error('Failed to load context stats:', err);
    } finally {
      setLoadingStats((prev) => ({ ...prev, [context.id]: false }));
    }
  };

  // Context Members Management
  const handleMembersClick = async (context: Context) => {
    setSelectedContext(context);
    setMembersDialogOpen(true);
    await loadContextMembers(context.id);
  };

  const loadContextMembers = async (contextId: string) => {
    setMembersLoading(true);
    try {
      const members = await listContextMembers(contextId);
      setContextMembers(members);
    } catch (error) {
      console.error('Failed to load context members:', error);
      toast({
        title: tCommon('error'),
        description: 'Failed to load members',
        variant: 'destructive',
      });
    } finally {
      setMembersLoading(false);
    }
  };

  const handleSaveApiKey = async () => {
    try {
      setApiKeySaving(true);
      setApiKeyError(null);

      // Validation
      if (!apiKeyValue.trim()) {
        setApiKeyError(t('apiKeyRequired'));
        return;
      }

      if (!apiKeyValue.startsWith('sk-')) {
        setApiKeyError(t('invalidApiKeyFormat'));
        return;
      }

      // Create OpenAI API key
      await createExternalAPIKey({
        key_name: 'OPENAI_API_KEY',
        provider: 'openai',
        value: apiKeyValue.trim(),
      });

      // Success
      toast({
        title: tCommon('success'),
        description: t('apiKeySaved'),
      });

      // Refresh OpenAI key status
      await checkApiKey();

      // Close dialog and reset
      setApiKeyDialogOpen(false);
      setApiKeyValue('');
      setApiKeyError(null);
    } catch (err: any) {
      console.error('Failed to save API key:', err);

      // Handle specific error codes
      let errorMessage = t('failedToSaveApiKey');
      if (err?.status === 409 || err?.details?.detail?.includes('already exists')) {
        errorMessage = t('apiKeyAlreadyExists');
      } else if (err?.details?.detail) {
        errorMessage = err.details.detail;
      } else if (err?.message) {
        errorMessage = err.message;
      }

      setApiKeyError(errorMessage);
      toast({
        title: tCommon('error'),
        description: errorMessage,
        variant: 'destructive',
      });
    } finally {
      setApiKeySaving(false);
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    if (locale === 'ja') {
      // Japanese format: 2025/12/20 22:30
      return date.toLocaleString('ja-JP', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      });
    }
    // English format: Dec 20, 2025, 10:30 PM
    return date.toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  return (
    <PageContainer>
      <PageHeader
        title={t('title')}
        description={t('subtitle')}
        actions={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={handleRefresh} disabled={loading}>
              <RefreshCw className={cn('h-4 w-4 mr-2', loading && 'animate-spin')} />
              {tCommon('refresh')}
            </Button>
            {/* Issue #169: Dropdown for Quick Create vs Advanced Create */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  size="sm"
                  disabled={
                    hasOpenAIKey === false ||
                    (currentWorkspace?.current_user_role === 'member' || currentWorkspace?.current_user_role === 'viewer') ||
                    isQuotaReached
                  }
                >
                  <Plus className="h-4 w-4 mr-2" />
                  {t('newContext')}
                  <ChevronDown className="h-4 w-4 ml-1" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <DropdownMenuItem onClick={() => {
                  if (isQuotaReached) {
                    setQuotaDialogOpen(true);
                  } else {
                    setQuickCreateDialogOpen(true);
                  }
                }}>
                  <Zap className="h-4 w-4 mr-2 text-amber-500" />
                  <div>
                    <div className="font-medium">{t('quickCreate')}</div>
                    <div className="text-xs text-muted-foreground">{t('quickCreateDesc')}</div>
                  </div>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => {
                  if (isQuotaReached) {
                    setQuotaDialogOpen(true);
                  } else {
                    setCreateDialogOpen(true);
                  }
                }}>
                  <Settings2 className="h-4 w-4 mr-2 text-blue-500" />
                  <div>
                    <div className="font-medium">{t('advancedCreate')}</div>
                    <div className="text-xs text-muted-foreground">{t('advancedCreateDesc')}</div>
                  </div>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        }
      />

      {/* Quota Warning (Issue #188) - Below header */}
      {isQuotaReached && (
        <div className="mb-6 text-sm text-yellow-600 dark:text-yellow-400 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-3">
          ⚠️ {t('quotaWarning')}{' '}
          <a href="/workspace/plan" className="underline hover:text-yellow-700 dark:hover:text-yellow-300 font-medium">
            {t('upgradePrompt')}
          </a>{' '}
          {t('upgradeToCreateMore')}
        </div>
      )}

      {/* Advanced Create Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t('createDialogTitle')}</DialogTitle>
            <DialogDescription>
              {t('createDialogDesc')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
                  <div className="space-y-2">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('contextName')}
                    </label>
                    <Input
                      placeholder={t('contextNamePlaceholder')}
                      value={newContextName}
                      onChange={(e) => setNewContextName(e.target.value.toLowerCase())}
                      className="font-mono"
                    />
                    <p className={cn(typography.caption, colors.text.muted)}>
                      {t('contextNameHelp')}
                    </p>
                  </div>
                  <div className="space-y-2">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('displayName')} <span className="text-gray-400">{t('summaryUsageTemplateOptional')}</span>
                    </label>
                    <Input
                      placeholder={t('displayNamePlaceholder')}
                      value={newContextDisplayName}
                      onChange={(e) => setNewContextDisplayName(e.target.value)}
                      maxLength={200}
                    />
                    <p className={cn(typography.caption, colors.text.muted)}>
                      {t('displayNameHelp')}
                    </p>
                  </div>
                  <div className="space-y-2">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('descriptionOptional')}
                    </label>
                    <Textarea
                      placeholder={t('contextDescriptionPlaceholder')}
                      value={newContextDescription}
                      onChange={(e) => setNewContextDescription(e.target.value)}
                      rows={3}
                    />
                  </div>

                  {/* Summary & Usage Guide Template */}
                  <div className="space-y-2">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('summaryUsageTemplate')} <span className="text-gray-400">{t('summaryUsageTemplateOptional')}</span>
                    </label>
                    <Select onValueChange={(templateId) => {
                      const template = getTemplate(templateId);
                      if (template) {
                        setNewContextSummary(template.summary);
                        setNewContextUsageGuide(template.usage_guide);
                      }
                    }}>
                      <SelectTrigger>
                        <SelectValue placeholder={t('templatePlaceholder')} />
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
                                <span className="text-xs text-muted-foreground ml-2">- {t.description}</span>
                              </div>
                            </div>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className={cn(typography.caption, colors.text.muted)}>
                      {t('customizeManually')}
                    </p>
                  </div>

                  {/* Summary for AI */}
                  <div className="space-y-2">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('summaryForAI')} <span className="text-gray-400">{t('summaryUsageTemplateOptional')}</span>
                    </label>
                    <Textarea
                      placeholder={t('summaryPlaceholder')}
                      value={newContextSummary}
                      onChange={(e) => setNewContextSummary(e.target.value)}
                      maxLength={500}
                      rows={2}
                    />
                    <p className={cn(typography.caption, colors.text.muted)}>
                      {t('summaryHelp')} {newContextSummary.length}/500
                    </p>
                  </div>

                  {/* Usage Guide for AI */}
                  <div className="space-y-2">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('usageGuideForAI')} <span className="text-gray-400">{t('summaryUsageTemplateOptional')}</span>
                    </label>
                    <Textarea
                      placeholder={t('usageGuidePlaceholder')}
                      value={newContextUsageGuide}
                      onChange={(e) => setNewContextUsageGuide(e.target.value)}
                      maxLength={2000}
                      rows={4}
                    />
                    <p className={cn(typography.caption, colors.text.muted)}>
                      {t('usageGuideHelp')} {newContextUsageGuide.length}/2000
                    </p>
                  </div>

                  {/* Privacy Control - Issue #182 */}
                  <div className="space-y-2">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('privacy')} <span className="text-red-500">{t('required')}</span>
                    </label>
                    <div className="space-y-2">
                      {/* Private Option */}
                      <label className={`flex items-start gap-3 p-3 border-2 rounded cursor-pointer ${
                        isPrivate
                          ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                          : 'border-gray-200 dark:border-gray-700'
                      } ${
                        currentWorkspace?.current_user_role === 'admin' ? 'opacity-60' : ''
                      }`}>
                        <input
                          type="radio"
                          value="private"
                          checked={isPrivate}
                          onChange={() => {
                            if (currentWorkspace?.current_user_role !== 'admin') {
                              setIsPrivate(true);
                            }
                          }}
                          disabled={currentWorkspace?.current_user_role === 'admin'}
                          className="mt-1"
                        />
                        <div className="flex-1">
                          <div className="font-medium text-sm flex items-center gap-2">
                            🔒 {t('privateOption')}
                            {currentWorkspace?.current_user_role === 'admin' && (
                              <Badge variant="outline" className="ml-1 text-xs bg-gray-100 text-gray-700">
                                {t('ownerOnly')}
                              </Badge>
                            )}
                          </div>
                          <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                            {currentWorkspace?.current_user_role === 'admin'
                              ? t('onlyOwnersCanCreatePrivate')
                              : t('privateAvailableAllPlans')
                            }
                          </div>
                        </div>
                      </label>

                      {/* Shared Option */}
                      <label className={`flex items-start gap-3 p-3 border-2 rounded ${
                        !isPrivate
                          ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/20'
                          : 'border-gray-200 dark:border-gray-700'
                      } ${
                        currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic'
                          ? 'opacity-60 cursor-not-allowed'
                          : 'cursor-pointer'
                      }`}>
                        <input
                          type="radio"
                          value="shared"
                          checked={!isPrivate}
                          onChange={() => {
                            // Issue #270: Only Pro plan can create shared contexts
                            if (currentWorkspace?.plan_name === 'pro') {
                              setIsPrivate(false);
                            }
                          }}
                          disabled={currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic'}
                          className="mt-1"
                        />
                        <div className="flex-1">
                          <div className="font-medium text-sm flex items-center gap-2">
                            👥 {t('sharedOption')}
                            {(currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic') && (
                              <Badge variant="outline" className="ml-1 text-xs bg-purple-100 text-purple-700">
                                {t('proPlan')}
                              </Badge>
                            )}
                          </div>
                          <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                            {currentWorkspace?.plan_name === 'pro'
                              ? t('teamMembersAccess')
                              : t('upgradeToPro')
                            }
                          </div>
                        </div>
                      </label>
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
                  <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>
                    {tCommon('cancel')}
                  </Button>
                  <Button onClick={handleCreateContext} disabled={creating}>
                    {creating && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
                    {creating ? t('creating') : t('create')}
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
        <Alert className={cn(
          hasOpenAIKey === false
            ? "bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800"
            : "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800"
        )}>
          {hasOpenAIKey === false ? (
            <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-500" />
          ) : (
            <Brain className="h-4 w-4 text-blue-600 dark:text-blue-500" />
          )}
          <AlertTitle className={cn(
            hasOpenAIKey === false
              ? "text-amber-900 dark:text-amber-100"
              : "text-blue-900 dark:text-blue-100"
          )}>
            {hasOpenAIKey === false ? t('setupNeededOpenAI') : t('noContextsYet')}
          </AlertTitle>
          <AlertDescription className={cn(
            hasOpenAIKey === false
              ? "text-amber-800 dark:text-amber-200"
              : "text-blue-800 dark:text-blue-200"
          )}>
            {hasOpenAIKey === false ? (
              <>
                {t('openAIKeyRequired')}
                <div className="flex gap-2 mt-3">
                  <Button
                    variant="outline"
                    size="sm"
                    className="border-amber-300 dark:border-amber-700 text-amber-900 dark:text-amber-100 hover:bg-amber-100 dark:hover:bg-amber-800"
                    onClick={() => setApiKeyDialogOpen(true)}
                  >
                    {t('configureApiKey')} →
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled
                    className="opacity-50 cursor-not-allowed"
                    onClick={() => {
                      toast({
                        title: t('apiKeyRequired'),
                        description: t('apiKeyRequiredDesc'),
                        variant: 'destructive',
                      });
                    }}
                  >
                    <Plus className="h-4 w-4 mr-2" />
                    {t('create')}
                  </Button>
                </div>
              </>
            ) : (
              <>
                {t('createFirstContext')}
                <div className="mt-3">
                  <Button
                    size="sm"
                    className="bg-blue-600 hover:bg-blue-700 text-white"
                    onClick={() => setCreateDialogOpen(true)}
                  >
                    <Plus className="h-4 w-4 mr-2" />
                    {t('create')}
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
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">{t('contextName')}</th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">{t('status')}</th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">{t('reranker')}</th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">{t('created')}</th>
                <th className="px-4 py-3 text-center font-medium text-gray-600 dark:text-gray-300">{tCommon('actions')}</th>
              </tr>
            </thead>
            <tbody>
              {contexts.map((context) => (
                <tr
                  key={context.id}
                  className={cn(
                    'border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/30',
                    context.id === user?.current_context_id && 'bg-brand-green-50 dark:bg-brand-green-900/10'
                  )}
                >
                  {/* Name */}
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <Brain className="h-4 w-4 text-brand-green-600 flex-shrink-0" />
                      <div>
                        <div className="font-medium text-gray-900 dark:text-gray-100">
                          {context.display_name || context.name}
                        </div>
                        {context.description && (
                          <div className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-[250px]" title={context.description}>
                            {context.description}
                          </div>
                        )}
                      </div>
                    </div>
                  </td>

                  {/* Status badges */}
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap items-center justify-center gap-1">
                      {context.is_default && (
                        <Badge variant="outline" className="text-xs bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-900/30 dark:text-amber-300 dark:border-amber-700" title={t('default')}>
                          {t('default')}
                        </Badge>
                      )}
                      {context.is_public && (
                        <Badge variant="outline" className="text-xs bg-gradient-to-r from-purple-100 to-blue-100 text-purple-700 border-purple-300 dark:from-purple-900/40 dark:to-blue-900/40 dark:text-purple-300" title="Public" aria-label="Public">
                          🌍
                        </Badge>
                      )}
                      {context.is_private ? (
                        <Badge variant="outline" className="text-xs bg-blue-50 text-blue-700 border-blue-200 dark:bg-blue-900/30 dark:text-blue-300 dark:border-blue-700" title={t('privateOption')} aria-label={t('privateOption')}>
                          🔒
                        </Badge>
                      ) : (
                        <Badge variant="outline" className="text-xs bg-purple-50 text-purple-700 border-purple-200 dark:bg-purple-900/30 dark:text-purple-300 dark:border-purple-700" title={t('sharedOption')} aria-label={t('sharedOption')}>
                          👥
                        </Badge>
                      )}
                    </div>
                  </td>

                  {/* Rerank toggle */}
                  <td className="px-4 py-3 text-center">
                    {currentWorkspace?.plan_name !== 'free' ? (
                      <button
                        disabled={actionLoading === context.id}
                        onClick={async () => {
                          try {
                            setActionLoading(context.id);
                            const config = await getContextSearchConfig(context.id);
                            await updateContextSearchConfig(context.id, {
                              ...config,
                              use_rerank: !config.use_rerank,
                            });
                            fetchContexts();
                          } catch {
                            toast({ title: t('rerankToggleFailed'), variant: 'destructive' });
                          } finally {
                            setActionLoading(null);
                          }
                        }}
                        className={cn(
                          'inline-flex items-center gap-1 px-2 py-1 rounded text-xs font-medium transition-colors disabled:opacity-50',
                          context.use_rerank
                            ? 'bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-300 dark:hover:bg-green-900/50'
                            : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400 dark:hover:bg-gray-700'
                        )}
                        title={context.use_rerank ? t('rerankOn') : t('rerankOff')}
                      >
                        {context.use_rerank ? `✓ ${t('enabled')}` : t('off')}
                      </button>
                    ) : (
                      <span className="text-xs text-gray-400">—</span>
                    )}
                  </td>

                  {/* Created */}
                  <td className="px-4 py-3 text-center text-xs text-gray-500 dark:text-gray-400">
                    {formatDate(context.created_at)}
                  </td>

                  {/* Actions */}
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-center gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 w-7 p-0"
                        onClick={() => handleViewStats(context)}
                        title={t('viewUsage')}
                      >
                        <BarChart className="h-3.5 w-3.5" />
                      </Button>
                      {context.created_by === user?.id && (
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="sm" className="h-7 w-7 p-0">
                              <MoreVertical className="h-3.5 w-3.5" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            <DropdownMenuItem onClick={() => handleEditClick(context)}>
                              <Edit className="h-4 w-4 mr-2" />
                              {tCommon('edit')}
                            </DropdownMenuItem>
                            <DropdownMenuItem onClick={() => router.push(`/workspace/contexts/${context.id}/search-settings`)}>
                              <Settings2 className="h-4 w-4 mr-2" />
                              {t('searchSettings')}
                            </DropdownMenuItem>
                            {!context.is_default && (
                              <>
                                <DropdownMenuSeparator />
                                <DropdownMenuItem
                                  onClick={() => handleDeleteClick(context)}
                                  disabled={context.id === user?.current_context_id}
                                  className="text-red-600 focus:text-red-600"
                                >
                                  <Trash2 className="h-4 w-4 mr-2" />
                                  {tCommon('delete')}
                                </DropdownMenuItem>
                              </>
                            )}
                          </DropdownMenuContent>
                        </DropdownMenu>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Edit Context Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t('editContextTitle', { name: contextToEdit?.name || '' })}</DialogTitle>
            <DialogDescription>
              {t('editContextDesc')}
            </DialogDescription>
          </DialogHeader>

          {/* Permission Notice (Issue #165 Phase 3) */}
          <Alert className="bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800">
            <Shield className="h-4 w-4 text-amber-600 dark:text-amber-400" />
            <AlertTitle className="text-amber-900 dark:text-amber-100">
              {t('permissionNotice')}
            </AlertTitle>
            <AlertDescription className="text-amber-800 dark:text-amber-200">
              {t('permissionNoticeDesc')}
            </AlertDescription>
          </Alert>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, 'font-medium')}>
                {t('displayName')}
              </label>
              <Input
                placeholder={t('displayNamePlaceholder')}
                value={editDisplayName}
                onChange={(e) => setEditDisplayName(e.target.value)}
                maxLength={200}
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {t('displayNameHelp')}
              </p>
            </div>
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, 'font-medium')}>
                {t('description')}
              </label>
              <Textarea
                placeholder={t('contextDescriptionPlaceholder')}
                value={editDescription}
                onChange={(e) => setEditDescription(e.target.value)}
                rows={2}
              />
            </div>

            {/* Summary & Usage Guide Template - Collapsible */}
            <div className="space-y-2">
              {!showTemplate ? (
                <button
                  onClick={() => setShowTemplate(true)}
                  className="text-sm text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 flex items-center gap-1"
                >
                  {t('useTemplate')} →
                </button>
              ) : (
                <div className="space-y-2 p-3 border rounded-lg bg-slate-50 dark:bg-slate-900/50">
                  <div className="flex items-center justify-between">
                    <label className={cn(typography.bodySmall, 'font-medium')}>
                      {t('summaryUsageTemplate')}
                    </label>
                    <button
                      onClick={() => setShowTemplate(false)}
                      className="text-xs text-gray-500 hover:text-gray-700"
                    >
                      {t('closeTemplate')} ×
                    </button>
                  </div>
                  <Select onValueChange={(templateId) => {
                    const template = getTemplate(templateId);
                    if (template) {
                      setEditSummary(template.summary);
                      setEditUsageGuide(template.usage_guide);
                      setShowTemplate(false);
                    }
                  }}>
                    <SelectTrigger>
                      <SelectValue placeholder={t('templatePlaceholder')} />
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
                              <span className="text-xs text-muted-foreground ml-2">- {t.description}</span>
                            </div>
                          </div>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
            </div>

            <div className="space-y-2">
              <label className={cn(typography.bodySmall, 'font-medium')}>
                {t('summaryForAI')}
              </label>
              <Textarea
                placeholder={t('summaryPlaceholder')}
                value={editSummary}
                onChange={(e) => setEditSummary(e.target.value)}
                maxLength={500}
                rows={2}
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {editSummary.length}/500
              </p>
            </div>

            <div className="space-y-2">
              <label className={cn(typography.bodySmall, 'font-medium')}>
                {t('usageGuideForAI')}
              </label>
              <Textarea
                placeholder={t('usageGuidePlaceholder')}
                value={editUsageGuide}
                onChange={(e) => setEditUsageGuide(e.target.value)}
                maxLength={2000}
                rows={6}
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {editUsageGuide.length}/2000
              </p>
            </div>

            {/* Privacy Control - Edit (Issue #184) */}
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, 'font-medium')}>
                {t('privacy')} <span className="text-yellow-600 dark:text-yellow-400">({t('ownerOnly')})</span>
              </label>
              {(editIsPublic || contextToEdit?.is_public) && (
                <Alert className="border-blue-200 bg-blue-50 dark:bg-blue-900/20">
                  <AlertCircle className="h-4 w-4 text-blue-600" />
                  <AlertDescription className="text-blue-700 dark:text-blue-300 text-xs">
                    {t('publicContextMustBeShared')}
                  </AlertDescription>
                </Alert>
              )}
              <div className="space-y-2">
                {/* Private Option */}
                <label className={`flex items-start gap-3 p-3 border-2 rounded ${
                  editIsPrivate
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                    : 'border-gray-200 dark:border-gray-700 cursor-pointer'
                } ${
                  (editIsPublic || contextToEdit?.is_public) ? 'opacity-50 cursor-not-allowed' : ''
                }`}>
                  <input
                    type="radio"
                    value="private"
                    checked={editIsPrivate}
                    onChange={() => setEditIsPrivate(true)}
                    disabled={editIsPublic || contextToEdit?.is_public}
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      🔒 {t('privateOption')}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {t('privateOptionDesc')}
                    </div>
                  </div>
                </label>

                {/* Shared Option */}
                <label className={`flex items-start gap-3 p-3 border-2 rounded ${
                  !editIsPrivate
                    ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/20'
                    : 'border-gray-200 dark:border-gray-700'
                } ${
                  (currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic') || (editIsPublic || contextToEdit?.is_public)
                    ? 'opacity-60 cursor-not-allowed'
                    : 'cursor-pointer'
                }`}>
                  <input
                    type="radio"
                    value="shared"
                    checked={!editIsPrivate}
                    onChange={() => {
                      // Issue #270: Only Pro plan can create shared contexts
                      if (currentWorkspace?.plan_name === 'pro' && !(editIsPublic || contextToEdit?.is_public)) {
                        setEditIsPrivate(false);
                      }
                    }}
                    disabled={(currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic') || (editIsPublic || contextToEdit?.is_public)}
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      👥 {t('sharedOption')}
                      {(currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic') && (
                        <Badge variant="outline" className="ml-1 text-xs bg-yellow-100 text-yellow-800">
                          {t('proPlan')}
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {currentWorkspace?.plan_name === 'pro'
                        ? t('allWorkspaceMembersCanAccess')
                        : t('upgradeToProShare')
                      }
                    </div>
                  </div>
                </label>
              </div>

              {/* Warning for Shared → Private transition */}
              {contextToEdit && !contextToEdit.is_private && editIsPrivate && (
                <Alert className="mt-2">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertDescription>
                    {t('changeToPrivateWarning')}
                  </AlertDescription>
                </Alert>
              )}

              {/* Warning for Private → Shared transition */}
              {contextToEdit && contextToEdit.is_private && !editIsPrivate && (
                <Alert className="mt-2">
                  <AlertTriangle className="h-4 w-4" />
                  <AlertDescription>
                    {t('changeToSharedWarning')}
                  </AlertDescription>
                </Alert>
              )}
            </div>

            {/* Public Access Control - Issue #238 - Owner only */}
            {!editIsPrivate && currentWorkspace?.current_user_role === 'owner' && (
              <details className="border border-slate-200 dark:border-slate-700 rounded-lg p-4">
                <summary className="cursor-pointer font-medium text-slate-700 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100 -m-4 p-4">
                  🌍 Public設定 (Owner only)
                </summary>
                <div className="space-y-3 mt-4">
                {/* Resource ID Prefix (only if not yet public) */}
                {!editIsPublic && !contextToEdit?.is_public && (
                  <div className="p-4 border-2 rounded-lg bg-blue-50 dark:bg-blue-950/20 border-blue-200 dark:border-blue-700 space-y-3">
                    <div>
                      <label className="text-sm font-semibold text-blue-900 dark:text-blue-100 mb-2 block">
                        {t('resourceIdPrefix')} <span className="text-red-500">*</span>
                      </label>
                      <Input
                        placeholder={t('resourceIdPrefixPlaceholder')}
                        value={editResourceIdPrefix}
                        onChange={(e) => {
                          const original = e.target.value;
                          // Auto-convert to lowercase and replace invalid chars
                          const value = original.toLowerCase().replace(/[^a-z0-9_]/g, '');
                          setEditResourceIdPrefix(value);
                        }}
                        className={`font-mono text-sm bg-white dark:bg-slate-900 ${
                          editResourceIdPrefix && editResourceIdPrefix !== editResourceIdPrefix.toLowerCase()
                            ? 'border-blue-400'
                            : ''
                        }`}
                      />
                      {editResourceIdPrefix && /[A-Z]/.test(editResourceIdPrefix) && (
                        <p className="text-xs text-blue-600 mt-1">
                          ℹ️ Converted to lowercase automatically
                        </p>
                      )}
                    </div>

                    <div className="space-y-2 text-xs">
                      <p className="text-blue-800 dark:text-blue-200">
                        <strong>{t('resourceIdInputLabel')}</strong>
                      </p>
                      <ul className="text-blue-700 dark:text-blue-300 space-y-1 list-disc list-inside">
                        <li>{t('resourceIdValidation')}</li>
                        <li>{t('resourceIdExamples', { examples: 'products, docs_articles, ec_inventory' })}</li>
                        <li>{t('resourceIdN1Design')}</li>
                      </ul>
                      <div className="mt-2 p-2 bg-blue-100 dark:bg-blue-900/30 rounded border border-blue-200 dark:border-blue-700">
                        <p className="text-blue-900 dark:text-blue-100 font-medium mb-1">{t('resourceIdN1Example')}</p>
                        <p className="text-blue-800 dark:text-blue-200">
                          {t('resourceIdN1ExampleDetail', { resourceId: 'products' })}<br />
                          {t('resourceIdN1TokenA')}<br />
                          {t('resourceIdN1TokenB')}<br />
                          {t('resourceIdN1Result')}
                        </p>
                      </div>
                      <p className="mt-2">
                        <Link href="/workspace/integrations/resource-tokens" className="text-blue-600 dark:text-blue-400 underline hover:text-blue-700 dark:hover:text-blue-300 font-medium">
                          {t('manageResourceTokensLink', { default: 'Manage Resource Tokens →' })}
                        </Link>
                      </p>
                    </div>
                  </div>
                )}

                {/* Public Toggle */}
                {editIsPublic ? (
                  <div className="space-y-3">
                    <Alert className="border-purple-200 bg-purple-50 dark:bg-purple-900/20">
                      <AlertCircle className="h-4 w-4 text-purple-600" />
                      <AlertTitle className="text-purple-800 dark:text-purple-200 text-sm">
                        🌍 {t('publicContextPermanent')}
                      </AlertTitle>
                      <div className="text-purple-700 dark:text-purple-300 text-xs space-y-2 mt-2">
                        <div>{t('publicExplanation')}</div>
                        <ul className="list-disc list-inside space-y-1 ml-2">
                          <li>{t('publicFeature1')}</li>
                          <li>{t('publicFeature2')}</li>
                        </ul>
                        {contextToEdit?.resource_id && (
                          <div className="mt-2 pt-2 border-t border-purple-200">
                            <span className="font-medium">{t('resourceIdPrefix')}: </span>
                            <code className="font-mono">{contextToEdit.resource_id}</code>
                          </div>
                        )}
                      </div>
                    </Alert>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={async () => {
                        // If already saved as public → fetch impact and show warning dialog
                        if (contextToEdit?.is_public) {
                          let tokenCount = 0;
                          if (contextToEdit.resource_id) {
                            try {
                              const impact = await getResourceImpact(contextToEdit.resource_id);
                              tokenCount = impact.token_count;
                            } catch {
                              tokenCount = 0;
                            }
                          }
                          setUnpublishTokenCount(tokenCount);
                          setUnpublishDialogOpen(true);
                        } else {
                          // Not yet saved → just toggle back (no warning)
                          setEditIsPublic(false);
                        }
                      }}
                      className="w-full border-red-300 text-red-700 hover:bg-red-50"
                    >
                      {t('unpublishContext')}
                    </Button>
                  </div>
                ) : contextToEdit?.is_public ? (
                  // Was public, now unpublished (not saved yet) - show "公開に戻す"
                  <div className="space-y-3">
                    <Alert className="border-gray-200 bg-gray-50 dark:bg-gray-900/20">
                      <AlertCircle className="h-4 w-4 text-gray-600" />
                      <AlertTitle className="text-gray-800 dark:text-gray-200 text-sm">
                        🔐 {t('unpublishedPending')}
                      </AlertTitle>
                      <div className="text-gray-700 dark:text-gray-300 text-xs mt-2">
                        <div>{t('unpublishedPendingDesc')}</div>
                        <ul className="list-disc list-inside space-y-1 ml-2 mt-1">
                          <li>Public Search API</li>
                          <li>Resource Ingest API</li>
                        </ul>
                      </div>
                    </Alert>
                    <button
                      onClick={() => setEditIsPublic(true)}
                      className="w-full px-4 py-3 rounded-lg border-2 border-dashed border-purple-300 dark:border-purple-700 bg-purple-50 dark:bg-purple-950/20 hover:bg-purple-100 dark:hover:bg-purple-900/30 transition-colors"
                    >
                      <div className="text-sm font-medium text-purple-700 dark:text-purple-300">
                        🌍 {t('republish')}
                      </div>
                      <div className="text-xs text-purple-600 dark:text-purple-400 mt-1">
                        {t('republishDesc')}
                      </div>
                    </button>
                  </div>
                ) : (
                  // Never been public - show "公開する" (first time)
                  <div className="space-y-3">
                    <Alert className="border-blue-200 bg-blue-50 dark:bg-blue-900/20">
                      <AlertCircle className="h-4 w-4 text-blue-600" />
                      <AlertTitle className="text-blue-800 dark:text-blue-200 text-sm">
                        {t('publicExplanation')}
                      </AlertTitle>
                      <div className="text-blue-700 dark:text-blue-300 text-xs mt-2">
                        <ul className="list-disc list-inside space-y-1">
                          <li>{t('publicFeature1')}</li>
                          <li>{t('publicFeature2')}</li>
                        </ul>
                      </div>
                    </Alert>
                    <button
                      onClick={() => setEditIsPublic(true)}
                      disabled={!editResourceIdPrefix.trim()}
                      className={cn(
                        "w-full px-4 py-3 rounded-lg border-2 border-dashed transition-colors",
                        !editResourceIdPrefix.trim()
                          ? "border-gray-300 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/20 opacity-50 cursor-not-allowed"
                          : "border-purple-300 dark:border-purple-700 bg-purple-50 dark:bg-purple-950/20 hover:bg-purple-100 dark:hover:bg-purple-900/30"
                      )}
                    >
                      <div className={cn(
                        "text-sm font-medium",
                        !editResourceIdPrefix.trim() ? "text-gray-500" : "text-purple-700 dark:text-purple-300"
                      )}>
                        🌍 {t('makePublic')}
                      </div>
                      <div className={cn(
                        "text-xs mt-1",
                        !editResourceIdPrefix.trim() ? "text-gray-400" : "text-purple-600 dark:text-purple-400"
                      )}>
                        {!editResourceIdPrefix.trim() ? t('resourceIdPrefixRequired') : t('makePublicDesc')}
                      </div>
                    </button>
                  </div>
                )}
                </div>
              </details>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              {tCommon('cancel')}
            </Button>
            <Button onClick={handleSaveEdit} disabled={editing}>
              {editing && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {editing ? t('updating') : tCommon('saveChanges')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      {/* Context Members Dialog */}
      <Dialog open={membersDialogOpen} onOpenChange={setMembersDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Users className="h-5 w-5 text-purple-500" />
              {t('contextMembers')} ({t('membersCount', { count: contextMembers.length })})
            </DialogTitle>
            <DialogDescription>
              {selectedContext?.display_name || selectedContext?.name}
            </DialogDescription>
          </DialogHeader>
          <div className="py-4">
            {membersLoading ? (
              <div className="flex justify-center py-8">
                <Loader2 className="h-6 w-6 animate-spin text-gray-400" />
              </div>
            ) : contextMembers.length === 0 ? (
              <div className="text-center py-8 text-gray-500">
                <Users className="h-12 w-12 mx-auto mb-2 opacity-20" />
                <p className="text-sm">{t('noMembersAssigned')}</p>
                <p className="text-xs mt-1">{t('onlyOwnerHasAccess')}</p>
                <p className="text-xs mt-2 text-blue-600 dark:text-blue-400">
                  {t('ownerAdminAutoAccess')}
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {contextMembers.map((member) => (
                  <div
                    key={member.user_id}
                    className="flex items-center justify-between p-3 border border-gray-200 dark:border-gray-700 rounded-lg"
                  >
                    <div className="flex-1">
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                        {member.user_name || member.user_email || member.user_id}
                      </p>
                      <p className="text-xs text-gray-500">
                        ID: {member.user_id}
                      </p>
                      <p className="text-xs text-gray-600 dark:text-gray-400 capitalize mt-0.5">
                        {member.role === 'owner' ? t('owner') :
                         member.role === 'admin' ? t('admin') :
                         member.role === 'member' ? t('member') :
                         member.role === 'viewer' ? t('viewer') :
                         member.role}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Link to workspace members page */}
            <div className="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
              <a
                href="/workspace/members"
                className="text-sm text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 flex items-center gap-1"
              >
                <Settings2 className="h-4 w-4" />
                {t('manageWorkspaceMembers')}
              </a>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('deleteContextTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('deleteContextMessage', { name: contextToDelete?.name || '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon('cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDelete}
              className="bg-red-600 hover:bg-red-700"
              disabled={deleting}
            >
              {deleting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {deleting ? t('deleting') : t('deleteContextTitle')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Issue #169: Quick Create Dialog */}
      <Dialog open={quickCreateDialogOpen} onOpenChange={setQuickCreateDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Zap className="h-5 w-5 text-amber-500" />
              {t('quickCreateContext')}
            </DialogTitle>
            <DialogDescription>
              {t('quickCreateContextDesc')}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, 'font-medium')}>
                {t('contextName')}
              </label>
              <Input
                placeholder={t('contextNamePlaceholder')}
                value={quickCreateName}
                onChange={(e) => setQuickCreateName(e.target.value.toLowerCase())}
                className="font-mono"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !quickCreating) {
                    handleQuickCreate();
                  }
                }}
                autoFocus
              />
              <p className={cn(typography.caption, colors.text.muted)}>
                {t('contextNameHelp')}
              </p>
            </div>

            {/* Privacy Control - Quick Create - Issue #182 */}
            <div className="space-y-2">
              <label className={cn(typography.bodySmall, 'font-medium')}>
                {t('privacy')}
              </label>
              <div className="space-y-2">
                {/* Private Option */}
                <label className={`flex items-start gap-3 p-3 border-2 rounded cursor-pointer ${
                  isPrivate
                    ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20'
                    : 'border-gray-200 dark:border-gray-700'
                } ${
                  currentWorkspace?.current_user_role === 'admin' ? 'opacity-60' : ''
                }`}>
                  <input
                    type="radio"
                    value="private"
                    checked={isPrivate}
                    onChange={() => {
                      if (currentWorkspace?.current_user_role !== 'admin') {
                        setIsPrivate(true);
                      }
                    }}
                    disabled={currentWorkspace?.current_user_role === 'admin'}
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      🔒 {t('privateOption')}
                      {currentWorkspace?.current_user_role === 'admin' && (
                        <Badge variant="outline" className="ml-1 text-xs bg-gray-100 text-gray-700">
                          {t('ownerOnly')}
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {currentWorkspace?.current_user_role === 'admin'
                        ? t('adminsCanOnlyCreateShared')
                        : t('onlyYouCanAccess')
                      }
                    </div>
                  </div>
                </label>

                {/* Shared Option */}
                <label className={`flex items-start gap-3 p-3 border-2 rounded ${
                  !isPrivate
                    ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/20'
                    : 'border-gray-200 dark:border-gray-700'
                } ${
                  currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic'
                    ? 'opacity-60 cursor-not-allowed'
                    : 'cursor-pointer'
                }`}>
                  <input
                    type="radio"
                    value="shared"
                    checked={!isPrivate}
                    onChange={() => {
                      // Issue #270: Only Pro plan can create shared contexts
                      if (currentWorkspace?.plan_name === 'pro') {
                        setIsPrivate(false);
                      }
                    }}
                    disabled={currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic'}
                    className="mt-1"
                  />
                  <div className="flex-1">
                    <div className="font-medium text-sm flex items-center gap-2">
                      👥 {t('sharedOption')}
                      {(currentWorkspace?.plan_name === 'free' || currentWorkspace?.plan_name === 'basic') && (
                        <Badge variant="outline" className="ml-1 text-xs bg-purple-100 text-purple-700">
                          {t('pro')}
                        </Badge>
                      )}
                    </div>
                    <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                      {currentWorkspace?.plan_name === 'pro'
                        ? t('teamMembersCanAccessShort')
                        : t('requiresProPlan')
                      }
                    </div>
                  </div>
                </label>
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
            <Button variant="outline" onClick={() => {
              setQuickCreateDialogOpen(false);
              setQuickCreateName('');
              setQuickCreateError(null);
            }}>
              {tCommon('cancel')}
            </Button>
            <Button onClick={handleQuickCreate} disabled={quickCreating}>
              {quickCreating && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              {quickCreating ? t('creating') : tCommon('create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>


      {/* OpenAI API Key Setup Dialog */}
      <Dialog open={apiKeyDialogOpen} onOpenChange={setApiKeyDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('configureOpenAIKey')}</DialogTitle>
            <DialogDescription>
              {t('openAIKeyDialogDesc')}
            </DialogDescription>
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
              <Label htmlFor="api-key">{t('openAIApiKey')}</Label>
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
                {t('openAIKeyHelp')}{' '}
                <a
                  href="https://platform.openai.com/api-keys"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-blue-600 hover:underline"
                >
                  {t('openAIPlatform')}
                </a>
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setApiKeyDialogOpen(false);
                setApiKeyValue('');
                setApiKeyError(null);
              }}
              disabled={apiKeySaving}
            >
              {tCommon('cancel')}
            </Button>
            <Button
              onClick={handleSaveApiKey}
              disabled={apiKeySaving || !apiKeyValue.trim()}
            >
              {apiKeySaving ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t('savingApiKey')}
                </>
              ) : (
                t('saveApiKey')
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
              Context Limit Reached
            </AlertDialogTitle>
            <AlertDialogDescription>
              You've reached the Free plan limit of <strong>1 context</strong>.
            </AlertDialogDescription>
            <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-3 mt-3">
              <p className="text-sm text-blue-900 dark:text-blue-100 font-medium mb-1">
                Upgrade to create more contexts
              </p>
              <p className="text-sm text-blue-800 dark:text-blue-200">
                Basic plan: <strong>10 contexts</strong><br />
                Pro plan: <strong>Unlimited contexts</strong>
              </p>
            </div>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => router.push('/admin/plans')}>
              View Plans
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Unpublish Confirmation Dialog - Issue #238 */}
      <AlertDialog open={unpublishDialogOpen} onOpenChange={setUnpublishDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-red-600">
              <AlertCircle className="h-5 w-5" />
              {t('unpublishContextTitle')}
            </AlertDialogTitle>
            <div className="space-y-3 text-sm text-muted-foreground">
              <div className="font-medium">{t('unpublishContextImpact')}</div>
              <ul className="list-disc list-inside space-y-1 text-sm">
                <li><strong>{t('unpublishImpact1')}</strong></li>
                <li>{t('unpublishImpact2')}</li>
                <li>{t('unpublishImpact3')}</li>
                <li>{t('unpublishImpact4')}</li>
              </ul>
              {unpublishTokenCount > 0 && (
                <Alert className="border-red-200 bg-red-50 dark:bg-red-900/20">
                  <AlertTriangle className="h-4 w-4 text-red-600" />
                  <AlertDescription className="text-red-800 dark:text-red-200 text-xs">
                    {t('unpublishTokenWarning', { count: unpublishTokenCount })}
                  </AlertDescription>
                </Alert>
              )}
              <Alert className="border-orange-200 bg-orange-50 dark:bg-orange-900/20">
                <AlertTriangle className="h-4 w-4 text-orange-600" />
                <AlertDescription className="text-orange-800 dark:text-orange-200 text-xs">
                  {t('unpublishWarning')}
                </AlertDescription>
              </Alert>
            </div>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon('cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setEditIsPublic(false);
                setUnpublishDialogOpen(false);
              }}
              className="bg-red-600 hover:bg-red-700"
            >
              {t('unpublishContext')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
