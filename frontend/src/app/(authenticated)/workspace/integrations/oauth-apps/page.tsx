/**
 * Custom OAuth Apps Page
 *
 * Manage OAuth applications for Claude, ChatGPT, and custom integrations
 *
 * Features:
 * - Display OAuth apps with zero-knowledge secrets
 * - 10-minute auto-hide countdown for secrets
 * - Create/Regenerate/Delete OAuth apps
 * - Claude, ChatGPT, and Custom app support
 */

'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useTranslations, useLocale } from 'next-intl';
import { PageHeader } from '@/components/common/PageHeader';
import { PageContainer } from '@/components/common/PageContainer';
import { Section } from '@/components/common/Section';
import { FeatureGuide } from '@/components/common/FeatureGuide';
import { ActionButton } from '@/components/common/ActionButton';
import { LoadingState } from '@/components/common/LoadingState';
import { ErrorBanner } from '@/components/common/ErrorBanner';
import { useWorkspace } from '@/contexts/WorkspaceContext';
import { useAuth } from '@/contexts/AuthContext';
import {
  getOAuth2Clients,
  createOAuth2Client,
  deleteOAuth2Client,
  regenerateOAuth2ClientSecret,
  OAuth2Client,
} from '@/lib/api/oauth';
import { hideOAuthClientSecret } from '@/lib/api/member-credentials';
import { Copy, Check, EyeOff, RefreshCw, Trash2, AlertTriangle, Plus, X } from 'lucide-react';
import { formatDateTime, formatRelativeTime } from '@/lib/utils/datetime';
import { useToast } from '@/hooks/use-toast';
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

// Auto-refresh interval: 5 minutes (refresh before 10-minute visibility expiry)
const OAUTH_REFRESH_INTERVAL_MS = 5 * 60 * 1000;

export default function CustomAppsPage() {
  const t = useTranslations('customApps');
  const tCommon = useTranslations('common');
  const locale = useLocale();
  const router = useRouter();

  const { currentWorkspaceId, currentWorkspace } = useWorkspace();
  const { user } = useAuth();
  const { toast } = useToast();

  // URLs
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8080';
  const baseUrl = apiUrl.replace(/\/api\/v1$/, '');
  const mcpBaseUrl = baseUrl + '/mcp';
  const workspaceScopedMcpUrl = currentWorkspaceId
    ? `${baseUrl}/mcp/w/${currentWorkspaceId}`
    : null;
  const oauthAuthorizeUrl = baseUrl + '/oauth/authorize';
  const oauthTokenUrl = baseUrl + '/oauth/token';

  const [oauthClients, setOauthClients] = useState<OAuth2Client[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copiedItems, setCopiedItems] = useState<Record<string, boolean>>({});

  // Custom OAuth App dialog state
  const [showCustomDialog, setShowCustomDialog] = useState(false);
  const [customAppName, setCustomAppName] = useState('');
  const [customRedirectUris, setCustomRedirectUris] = useState<string[]>(['']);
  const [customDialogError, setCustomDialogError] = useState<string | null>(null);

  // Confirmation dialog states
  const [showHideOAuthDialog, setShowHideOAuthDialog] = useState(false);
  const [oauthToHide, setOauthToHide] = useState<string | null>(null);
  const [showRegenerateOAuthDialog, setShowRegenerateOAuthDialog] = useState(false);
  const [oauthToRegenerate, setOauthToRegenerate] = useState<{ clientId: string; provider: string } | null>(null);
  const [showDeleteOAuthDialog, setShowDeleteOAuthDialog] = useState(false);
  const [oauthToDelete, setOauthToDelete] = useState<string | null>(null);

  // Track if component is mounted
  const isMountedRef = useRef(true);
  const copyTimeoutRef = useRef<NodeJS.Timeout | undefined>(undefined);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
      if (copyTimeoutRef.current) {
        clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  const loadOAuthClients = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const clients = await getOAuth2Clients();
      if (isMountedRef.current) {
        setOauthClients(clients);
      }
    } catch (err: any) {
      console.error('Failed to load OAuth clients:', err);
      if (isMountedRef.current) {
        setError(err.message);
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    if (currentWorkspaceId) {
      loadOAuthClients();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWorkspaceId]);

  // Auto-refresh every 5 minutes
  useEffect(() => {
    if (!currentWorkspaceId) return;

    const interval = setInterval(() => {
      loadOAuthClients();
    }, OAUTH_REFRESH_INTERVAL_MS);

    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWorkspaceId]);

  const handleCopy = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedItems(prev => ({ ...prev, [key]: true }));

      // Clear previous timeout
      if (copyTimeoutRef.current) {
        clearTimeout(copyTimeoutRef.current);
      }

      copyTimeoutRef.current = setTimeout(() => {
        if (isMountedRef.current) {
          setCopiedItems(prev => ({ ...prev, [key]: false }));
        }
      }, 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  const handleCreateOAuthApp = async (provider: 'claude' | 'chatgpt' | 'custom') => {
    if (provider === 'custom') {
      setShowCustomDialog(true);
      return;
    }

    try {
      setError(null);
      const result = await createOAuth2Client({
        provider: provider,  // バックエンドはproviderフィールドを期待
        client_name: provider === 'claude' ? 'Claude' : 'ChatGPT',  // client_nameが正しい
        redirect_uris: provider === 'claude'
          ? ['https://claude.ai/api/mcp/auth_callback']
          : ['https://chatgpt.com/connector_platform_oauth_redirect'],
      });

      await loadOAuthClients();

      toast({
        title: tCommon('success'),
        description: t('createSuccess', { provider: provider === 'claude' ? 'Claude' : 'ChatGPT' }),
      });
    } catch (err: any) {
      const errorMsg = err?.message || err?.details?.detail || JSON.stringify(err);
      setError(`Failed to create OAuth app: ${errorMsg}`);
    }
  };

  const handleCreateCustomOAuthApp = async () => {
    try {
      setCustomDialogError(null);

      if (!customAppName.trim()) {
        setCustomDialogError(t('appNameRequired'));
        return;
      }

      const validUris = customRedirectUris.filter(uri => uri.trim());
      if (validUris.length === 0) {
        setCustomDialogError(t('redirectUriRequired'));
        return;
      }

      await createOAuth2Client({
        provider: 'custom',  // バックエンドはproviderフィールドを期待
        client_name: customAppName,  // client_nameが正しい
        redirect_uris: validUris,
      });

      await loadOAuthClients();
      setShowCustomDialog(false);
      setCustomAppName('');
      setCustomRedirectUris(['']);

      toast({
        title: tCommon('success'),
        description: 'Custom OAuth app created successfully',
      });
    } catch (err: any) {
      setCustomDialogError(err.message || 'Failed to create OAuth app');
    }
  };

  const handleHideOAuthAppClick = (clientId: string) => {
    setOauthToHide(clientId);
    setShowHideOAuthDialog(true);
  };

  const handleConfirmHideOAuthApp = async () => {
    if (!oauthToHide) return;

    try {
      await hideOAuthClientSecret(oauthToHide);
      await loadOAuthClients();
      setShowHideOAuthDialog(false);
    } catch (err: any) {
      setError(`Failed to hide OAuth app: ${err.message}`);
    }
  };

  const handleRegenerateOAuthClick = (clientId: string, provider: string) => {
    setOauthToRegenerate({ clientId, provider });
    setShowRegenerateOAuthDialog(true);
  };

  const handleConfirmRegenerateOAuth = async () => {
    if (!oauthToRegenerate) return;

    try {
      await regenerateOAuth2ClientSecret(oauthToRegenerate.clientId);
      await loadOAuthClients();
      setShowRegenerateOAuthDialog(false);

      toast({
        title: tCommon('success'),
        description: `OAuth secret regenerated successfully`,
      });
    } catch (err: any) {
      setError(`Failed to regenerate OAuth secret: ${err.message}`);
    }
  };

  const handleDeleteOAuthClientClick = (clientId: string) => {
    setOauthToDelete(clientId);
    setShowDeleteOAuthDialog(true);
  };

  const handleConfirmDeleteOAuthClient = async () => {
    if (!oauthToDelete) return;

    try {
      await deleteOAuth2Client(oauthToDelete);
      await loadOAuthClients();
      setShowDeleteOAuthDialog(false);

      toast({
        title: tCommon('success'),
        description: 'OAuth app deleted successfully',
      });
    } catch (err: any) {
      setError(`Failed to delete OAuth app: ${err.message}`);
    }
  };

  if (loading && oauthClients.length === 0) {
    return <LoadingState lines={3} />;
  }

  const claudeApp = oauthClients.find(c => c.provider === 'claude');
  const chatgptApp = oauthClients.find(c => c.provider === 'chatgpt');
  const customApps = oauthClients.filter(c => c.provider === 'custom');

  return (
    <PageContainer>
      <PageHeader
        title={t('title', { default: 'App Authentication' })}
        description={t('description')}
      />

      <FeatureGuide storageKey="oauth-apps" title={t('featureGuide.title')}>
        <p>{t('featureGuide.overview')}</p>
        <p>{t('featureGuide.useCases')}</p>
        <p className="font-medium">{t('featureGuide.howItWorks')}</p>
      </FeatureGuide>

      <ErrorBanner error={error} />

      {/* MCP Connection URL */}
      <Section
        title={`🔗 ${t('mcpConnection', { default: 'MCP Connection' })}`}
        description={t('mcpConnectionDesc', { default: 'MCP endpoint URL for all clients' })}
      >
        <div className="space-y-3">
          {/* Main: Workspace-Scoped URL */}
          {workspaceScopedMcpUrl ? (
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-blue-50 dark:bg-blue-900/30 px-4 py-3 rounded border border-blue-200 dark:border-blue-800 text-sm font-mono text-blue-800 dark:text-blue-200">
                {workspaceScopedMcpUrl}
              </code>
              <button
                onClick={() => handleCopy(workspaceScopedMcpUrl, 'workspace-mcp-url')}
                className="p-3 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-800 rounded transition-colors"
                title={t('copyMcpUrl', { default: 'Copy MCP URL' })}
              >
                {copiedItems['workspace-mcp-url'] ? (
                  <Check className="w-4 h-4 text-green-600" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-blue-50 dark:bg-blue-900/30 px-4 py-3 rounded border border-blue-200 dark:border-blue-800 text-sm font-mono text-blue-800 dark:text-blue-200">
                {mcpBaseUrl}
              </code>
              <button
                onClick={() => handleCopy(mcpBaseUrl, 'mcp-url')}
                className="p-3 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-800 rounded transition-colors"
                title={t('copyMcpUrl', { default: 'Copy MCP URL' })}
              >
                {copiedItems['mcp-url'] ? (
                  <Check className="w-4 h-4 text-green-600" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </button>
            </div>
          )}
        </div>
      </Section>

      {/* OAuth Applications */}
      <Section>
        <div className="space-y-6">
          {/* Claude & ChatGPT Apps */}
          {[
            { provider: 'claude', app: claudeApp, icon: '🧠', title: t('claude'), subtitle: t('claudeSubtitle'), color: 'orange' },
            { provider: 'chatgpt', app: chatgptApp, icon: '🤖', title: t('chatgpt'), subtitle: t('chatgptSubtitle'), color: 'emerald' },
          ].map(({ provider, app, icon, title, subtitle }) => (
            <div key={provider} className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
              <div className="flex items-center gap-2 mb-3">
                <span className="text-2xl">{icon}</span>
                <div>
                  <h3 className="font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
                  <p className="text-xs text-gray-500 dark:text-gray-400">{subtitle}</p>
                </div>
              </div>

              {app ? (
                <div className="space-y-3">
                  {/* Client ID */}
                  <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('clientId')}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <code className="flex-1 bg-white dark:bg-gray-900 px-2 py-1 rounded text-xs font-mono break-all">
                        {app.client_id}
                      </code>
                      <button
                        onClick={() => handleCopy(app.client_id, `${provider}-id`)}
                        className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                        title={t('copyClientId')}
                      >
                        {copiedItems[`${provider}-id`] ? (
                          <Check className="w-4 h-4 text-green-600" />
                        ) : (
                          <Copy className="w-4 h-4" />
                        )}
                      </button>
                    </div>
                    <div className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                      <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">{t('redirectUri')}:</p>
                      {app.redirect_uris.map((uri, idx) => (
                        <p key={idx} className="text-xs text-gray-600 dark:text-gray-400 font-mono break-all">
                          {uri}
                        </p>
                      ))}
                    </div>
                  </div>

                  {/* Client Secret */}
                  {app.is_visible && app.plaintext_secret ? (
                    <div className="bg-green-50 dark:bg-green-900/20 p-3 rounded border border-green-200 dark:border-green-800">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('clientSecret')}</span>
                        {app.visibility_expires_at && new Date(app.visibility_expires_at) > new Date() && (
                          <span className="text-xs text-yellow-600 dark:text-yellow-400 flex items-center gap-1">
                            <AlertTriangle className="w-3 h-3" />
                            {t('hideInTime', { time: formatRelativeTime(app.visibility_expires_at, user?.timezone, locale) })}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 bg-white dark:bg-gray-900 px-2 py-1 rounded text-xs font-mono break-all">
                          {app.plaintext_secret}
                        </code>
                        <button
                          onClick={() => handleCopy(app.plaintext_secret!, `${provider}-secret`)}
                          className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                          title={t('copyClientSecret')}
                        >
                          {copiedItems[`${provider}-secret`] ? (
                            <Check className="w-4 h-4 text-green-600" />
                          ) : (
                            <Copy className="w-4 h-4" />
                          )}
                        </button>
                        <button
                          onClick={() => handleHideOAuthAppClick(app.client_id)}
                          className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                          title={t('hideSecretNow')}
                        >
                          <EyeOff className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700">
                      <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
                        <EyeOff className="w-4 h-4" />
                        <span className="text-sm">{t('secretHiddenOwner')}</span>
                      </div>
                    </div>
                  )}

                  {/* Actions */}
                  <div className="flex gap-2">
                    <ActionButton
                      onClick={() => handleRegenerateOAuthClick(app.client_id, title)}
                      icon={<RefreshCw className="w-4 h-4" />}
                    >
                      {t('regenerate')}
                    </ActionButton>
                    <ActionButton
                      onClick={() => handleDeleteOAuthClientClick(app.client_id)}
                      variant="danger"
                      icon={<Trash2 className="w-4 h-4" />}
                    >
                      {tCommon('delete')}
                    </ActionButton>
                  </div>

                  {/* Metadata */}
                  <div className="text-xs text-gray-500 dark:text-gray-400">
                    <p>{t('created')}: {formatDateTime(app.created_at, user?.timezone)}</p>
                  </div>
                </div>
              ) : (
                <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700">
                  <p className="text-sm text-gray-500 dark:text-gray-400 mb-3">
                    {t('noOAuthApp', { provider: title })}
                  </p>
                  <ActionButton
                    onClick={() => handleCreateOAuthApp(provider as 'claude' | 'chatgpt')}
                    icon={<Plus className="w-4 h-4" />}
                  >
                    {t('createOAuthApp', { provider: title })}
                  </ActionButton>
                </div>
              )}
            </div>
          ))}

          {/* Custom OAuth Apps */}
          <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className="text-2xl">🔧</span>
                <div>
                  <h3 className="font-semibold text-gray-900 dark:text-gray-100">{t('customOAuthApps')}</h3>
                  <p className="text-xs text-gray-500 dark:text-gray-400">{t('customOAuthAppsDesc')}</p>
                </div>
              </div>
              <ActionButton
                onClick={() => handleCreateOAuthApp('custom')}
                icon={<Plus className="w-4 h-4" />}
                variant="primary"
              >
                {t('createCustomApp')}
              </ActionButton>
            </div>

            {customApps.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-4">
                {t('noCustomApps')}
              </p>
            ) : (
              <div className="space-y-4">
                {customApps.map((app) => (
                  <div key={app.client_id} className="border-t border-gray-200 dark:border-gray-700 pt-4 first:border-t-0 first:pt-0">
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="font-semibold text-gray-900 dark:text-gray-100">{app.client_name}</h4>
                    </div>

                    {/* Client ID */}
                    <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700 mb-2">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('clientId')}</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 bg-white dark:bg-gray-900 px-2 py-1 rounded text-xs font-mono break-all">
                          {app.client_id}
                        </code>
                        <button
                          onClick={() => handleCopy(app.client_id, `custom-${app.client_id}-id`)}
                          className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                          title={t('copyClientId')}
                        >
                          {copiedItems[`custom-${app.client_id}-id`] ? (
                            <Check className="w-4 h-4 text-green-600" />
                          ) : (
                            <Copy className="w-4 h-4" />
                          )}
                        </button>
                      </div>
                      <div className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
                        <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">{t('redirectUri')}:</p>
                        {app.redirect_uris.map((uri, idx) => (
                          <p key={idx} className="text-xs text-gray-600 dark:text-gray-400 font-mono break-all">
                            {uri}
                          </p>
                        ))}
                      </div>
                    </div>

                    {/* Client Secret */}
                    {app.is_visible && app.plaintext_secret ? (
                      <div className="bg-green-50 dark:bg-green-900/20 p-3 rounded border border-green-200 dark:border-green-800 mb-2">
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{t('clientSecret')}</span>
                          {app.visibility_expires_at && new Date(app.visibility_expires_at) > new Date() && (
                            <span className="text-xs text-yellow-600 dark:text-yellow-400 flex items-center gap-1">
                              <AlertTriangle className="w-3 h-3" />
                              {t('hideInTime', { time: formatRelativeTime(app.visibility_expires_at, user?.timezone, locale) })}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <code className="flex-1 bg-white dark:bg-gray-900 px-2 py-1 rounded text-xs font-mono break-all">
                            {app.plaintext_secret}
                          </code>
                          <button
                            onClick={() => handleCopy(app.plaintext_secret!, `custom-${app.client_id}-secret`)}
                            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                            title={t('copyClientSecret')}
                          >
                            {copiedItems[`custom-${app.client_id}-secret`] ? (
                              <Check className="w-4 h-4 text-green-600" />
                            ) : (
                              <Copy className="w-4 h-4" />
                            )}
                          </button>
                          <button
                            onClick={() => handleHideOAuthAppClick(app.client_id)}
                            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
                            title={t('hideSecretNow')}
                          >
                            <EyeOff className="w-4 h-4" />
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700 mb-2">
                        <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
                          <EyeOff className="w-4 h-4" />
                          <span className="text-sm">{t('secretHiddenOwner')}</span>
                        </div>
                      </div>
                    )}

                    {/* Actions */}
                    <div className="flex gap-2">
                      <ActionButton
                        onClick={() => handleRegenerateOAuthClick(app.client_id, app.client_name)}
                        icon={<RefreshCw className="w-4 h-4" />}
                      >
                        {t('regenerate')}
                      </ActionButton>
                      <ActionButton
                        onClick={() => handleDeleteOAuthClientClick(app.client_id)}
                        variant="danger"
                        icon={<Trash2 className="w-4 h-4" />}
                      >
                        {tCommon('delete')}
                      </ActionButton>
                    </div>

                    {/* Metadata */}
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      <p>{t('created')}: {formatDateTime(app.created_at, user?.timezone)}</p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </Section>

      {/* Create Custom OAuth App Dialog */}
      <AlertDialog open={showCustomDialog} onOpenChange={setShowCustomDialog}>
        <AlertDialogContent className="max-w-2xl">
          <AlertDialogHeader>
            <AlertDialogTitle>{t('createCustomOAuthTitle')}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium mb-1">{t('appNameLabel')}</label>
                  <input
                    type="text"
                    value={customAppName}
                    onChange={(e) => setCustomAppName(e.target.value)}
                    placeholder={t('appNamePlaceholder')}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium mb-1">{t('redirectUris')}</label>
                  {customRedirectUris.map((uri, index) => (
                    <div key={index} className="flex items-center gap-2 mb-2">
                      <input
                        type="text"
                        value={uri}
                        onChange={(e) => {
                          const newUris = [...customRedirectUris];
                          newUris[index] = e.target.value;
                          setCustomRedirectUris(newUris);
                        }}
                        placeholder={t('redirectUriPlaceholder')}
                        className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 text-sm"
                      />
                      {customRedirectUris.length > 1 && (
                        <button
                          onClick={() => {
                            const newUris = customRedirectUris.filter((_, i) => i !== index);
                            setCustomRedirectUris(newUris);
                          }}
                          className="p-2 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
                          title={t('removeUri')}
                        >
                          <X className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  ))}
                  <button
                    onClick={() => setCustomRedirectUris([...customRedirectUris, ''])}
                    className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                  >
                    + {t('addRedirectUri')}
                  </button>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                    💡 {t('redirectUriHint')}
                  </p>
                </div>

                {customDialogError && (
                  <p className="text-sm text-red-600 dark:text-red-400">{customDialogError}</p>
                )}
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => {
              setShowCustomDialog(false);
              setCustomAppName('');
              setCustomRedirectUris(['']);
              setCustomDialogError(null);
            }}>
              {tCommon('cancel')}
            </AlertDialogCancel>
            <AlertDialogAction onClick={handleCreateCustomOAuthApp}>
              {t('createApp')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Hide OAuth Secret Dialog */}
      <AlertDialog open={showHideOAuthDialog} onOpenChange={setShowHideOAuthDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('hideOAuthTitle')}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>{t('hideOAuthWarning')}</p>
                <p>{t('hideOAuthNote')}</p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon('cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmHideOAuthApp}>
              {t('hideSecret')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Regenerate OAuth Secret Dialog */}
      <AlertDialog open={showRegenerateOAuthDialog} onOpenChange={setShowRegenerateOAuthDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('regenerateOAuthTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('regenerateOAuthDesc', { provider: oauthToRegenerate?.provider || '' })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon('cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmRegenerateOAuth}>
              {t('regenerate')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete OAuth App Dialog */}
      <AlertDialog open={showDeleteOAuthDialog} onOpenChange={setShowDeleteOAuthDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('deleteOAuthTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('deleteOAuthDesc')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon('cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmDeleteOAuthClient} className="bg-red-600 hover:bg-red-700">
              {tCommon('delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
