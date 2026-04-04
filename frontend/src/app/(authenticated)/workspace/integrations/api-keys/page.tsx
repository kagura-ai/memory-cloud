/**
 * API Keys Page
 *
 * Manage MCP API keys for Claude Code and other clients
 *
 * Features:
 * - Display API keys with zero-knowledge visibility
 * - 10-minute auto-hide countdown
 * - Create/Regenerate/Delete API keys
 */

"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useTranslations, useLocale } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import { FeatureGuide } from "@/components/common/FeatureGuide";
import { ActionButton } from "@/components/common/ActionButton";
import { LoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useAuth } from "@/contexts/AuthContext";
import {
  getMemberCredentials,
  hideAPIKey,
  regenerateAPIKey,
  deleteWorkspaceMemberAPIKey,
  createAPIKey,
  MemberCredentials,
} from "@/lib/api/member-credentials";
import {
  Copy,
  Check,
  EyeOff,
  RefreshCw,
  Trash2,
  AlertTriangle,
  Plus,
} from "lucide-react";
import { formatDateTime, formatRelativeTime } from "@/lib/utils/datetime";
import { useToast } from "@/hooks/use-toast";
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

// Auto-refresh interval: 5 minutes (refresh before 10-minute visibility expiry)
const CREDENTIALS_REFRESH_INTERVAL_MS = 5 * 60 * 1000;

export default function APIKeysPage() {
  const t = useTranslations("apiKeys");
  const tCommon = useTranslations("common");
  const locale = useLocale();
  const router = useRouter();

  const { currentWorkspaceId, currentWorkspace } = useWorkspace();
  const { user } = useAuth();
  const { toast } = useToast();

  const userId = user?.id;

  // URLs
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
  const baseUrl = apiUrl.replace(/\/api\/v1$/, "");
  const mcpBaseUrl = baseUrl + "/mcp";
  const workspaceScopedMcpUrl = currentWorkspaceId
    ? `${baseUrl}/mcp/w/${currentWorkspaceId}`
    : null;

  const [credentials, setCredentials] = useState<MemberCredentials | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copiedItems, setCopiedItems] = useState<Record<string, boolean>>({});

  // Dialog states
  const [showCreateKeyDialog, setShowCreateKeyDialog] = useState(false);
  const [newKeyName, setNewKeyName] = useState("");
  const [createKeyError, setCreateKeyError] = useState<string | null>(null);
  const [showHideApiKeyDialog, setShowHideApiKeyDialog] = useState(false);
  const [showRegenerateApiKeyDialog, setShowRegenerateApiKeyDialog] =
    useState(false);
  const [showDeleteApiKeyDialog, setShowDeleteApiKeyDialog] = useState(false);
  const [selectedKeyId, setSelectedKeyId] = useState<number | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Track if component is mounted to prevent state updates after unmount
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

  const loadCredentials = useCallback(async () => {
    if (!currentWorkspaceId || !userId) return;

    try {
      setLoading(true);
      setError(null);
      const data = await getMemberCredentials(currentWorkspaceId, userId);
      if (isMountedRef.current) {
        setCredentials(data);
      }
    } catch (err: any) {
      console.error("Failed to load credentials:", err);
      if (isMountedRef.current) {
        setError(err.message);
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
      }
    }
  }, [currentWorkspaceId, userId]);

  useEffect(() => {
    if (currentWorkspaceId && userId) {
      loadCredentials();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWorkspaceId, userId]);

  // Auto-refresh every 5 minutes
  useEffect(() => {
    if (!currentWorkspaceId || !userId) return;

    const interval = setInterval(() => {
      loadCredentials();
    }, CREDENTIALS_REFRESH_INTERVAL_MS);

    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWorkspaceId, userId]);

  const handleCopy = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedItems((prev) => ({ ...prev, [key]: true }));

      // Clear previous timeout
      if (copyTimeoutRef.current) {
        clearTimeout(copyTimeoutRef.current);
      }

      copyTimeoutRef.current = setTimeout(() => {
        if (isMountedRef.current) {
          setCopiedItems((prev) => ({ ...prev, [key]: false }));
        }
      }, 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  const handleHideAPIKeyClick = (keyId: number) => {
    setSelectedKeyId(keyId);
    setShowHideApiKeyDialog(true);
  };

  const handleConfirmHideAPIKey = async () => {
    if (!currentWorkspaceId || !userId) return;

    try {
      await hideAPIKey(currentWorkspaceId, userId);
      await loadCredentials();
      setShowHideApiKeyDialog(false);
    } catch (err: any) {
      toast({
        title: tCommon("error"),
        description: err.message,
        variant: "destructive",
      });
    }
  };

  const handleRegenerateAPIKeyClick = (keyId: number) => {
    setSelectedKeyId(keyId);
    setShowRegenerateApiKeyDialog(true);
  };

  const handleConfirmRegenerateAPIKey = async () => {
    if (!currentWorkspaceId || !userId) return;

    try {
      setRegenerating(true);
      await regenerateAPIKey(currentWorkspaceId, userId);
      await loadCredentials();
      setShowRegenerateApiKeyDialog(false);
      toast({
        title: tCommon("success"),
        description: t("regenerateSuccess"),
      });
    } catch (err: any) {
      setError(`Failed to regenerate API key: ${err.message}`);
    } finally {
      setRegenerating(false);
    }
  };

  const handleDeleteAPIKeyClick = (keyId: number) => {
    setSelectedKeyId(keyId);
    setShowDeleteApiKeyDialog(true);
  };

  const handleConfirmDeleteAPIKey = async () => {
    if (!currentWorkspaceId || !userId || !selectedKeyId) return;

    try {
      setDeleting(true);
      await deleteWorkspaceMemberAPIKey(
        currentWorkspaceId,
        userId,
        selectedKeyId,
      );
      await loadCredentials();
      setShowDeleteApiKeyDialog(false);
      toast({
        title: tCommon("success"),
        description: t("deleteSuccess"),
      });
    } catch (err: any) {
      setError(`Failed to delete API key: ${err.message}`);
    } finally {
      setDeleting(false);
    }
  };

  const handleCreateAPIKey = async () => {
    if (!currentWorkspaceId || !userId) return;

    try {
      setCreateKeyError(null);

      if (!newKeyName.trim()) {
        setCreateKeyError(t("keyNameRequired"));
        return;
      }

      await createAPIKey(currentWorkspaceId, userId, { name: newKeyName });
      await loadCredentials();
      setShowCreateKeyDialog(false);
      setNewKeyName("");

      toast({
        title: tCommon("success"),
        description: t("createSuccess"),
      });
    } catch (err: any) {
      setCreateKeyError(err.message || "Failed to create API key");
    }
  };

  if (loading && !credentials) {
    return <LoadingState lines={3} />;
  }

  const apiKeys = credentials?.api_keys || [];

  return (
    <PageContainer>
      <PageHeader title={t("title")} description={t("description")} />

      <FeatureGuide storageKey="api-keys" title={t("featureGuide.title")}>
        <p>{t("featureGuide.overview")}</p>
        <p>{t("featureGuide.useCases")}</p>
        <p className="font-medium">{t("featureGuide.howItWorks")}</p>
      </FeatureGuide>

      <ErrorBanner error={error} />

      {/* MCP OAuth Setup Link */}
      <div className="mb-6 p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
        <p className="text-sm font-medium text-blue-900 dark:text-blue-100 mb-2">
          🔗 {t("mcpOAuthSetup")}
        </p>
        <p className="text-xs text-blue-700 dark:text-blue-300 mb-3">
          {t("mcpOAuthSetupDesc")}
        </p>
        <Link href="/workspace/integrations/oauth-apps">
          <button className="text-sm text-blue-600 dark:text-blue-400 hover:underline font-medium">
            {t("goToOAuthApps")}
          </button>
        </Link>
      </div>

      {/* API Keys Section */}
      <Section title={t("apiKeysTitle")} description={t("apiKeysDesc")}>
        <div className="space-y-4">
          {/* MCP Setup Guide (Collapsible) */}
          <details className="border border-blue-200 dark:border-blue-800 rounded-lg">
            <summary className="cursor-pointer px-4 py-3 bg-blue-50 dark:bg-blue-900/20 hover:bg-blue-100 dark:hover:bg-blue-900/30 rounded-lg font-medium text-blue-900 dark:text-blue-100 text-sm">
              📖 {t("mcpSetupGuide")}
            </summary>
            <div className="p-4 bg-blue-50 dark:bg-blue-900/20 space-y-4">
              {/* MCP URL */}
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  MCP URL:
                </span>
                <code className="flex-1 bg-blue-100 dark:bg-blue-900/40 px-2 py-1 rounded border border-blue-200 dark:border-blue-800 text-xs font-mono text-blue-800 dark:text-blue-200">
                  {workspaceScopedMcpUrl || mcpBaseUrl}
                </code>
                <button
                  onClick={() =>
                    handleCopy(workspaceScopedMcpUrl || mcpBaseUrl, "mcp-url")
                  }
                  className="p-1.5 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-800 rounded transition-colors"
                  title={t("copyMcpUrl")}
                >
                  {copiedItems["mcp-url"] ? (
                    <Check className="w-3.5 h-3.5 text-green-600" />
                  ) : (
                    <Copy className="w-3.5 h-3.5" />
                  )}
                </button>
              </div>

              {/* Config Example */}
              <div>
                <p className="font-semibold text-gray-900 dark:text-gray-100 mb-2 text-sm">
                  {t("mcpConfigTitle")}
                </p>
                <pre className="bg-gray-900 text-gray-100 p-3 rounded overflow-x-auto text-xs">
                  {`{
  "mcpServers": {
    "kagura-memory": {
      "type": "http",
      "url": "${workspaceScopedMcpUrl || mcpBaseUrl}",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}`}
                </pre>
                <p className="text-xs text-blue-700 dark:text-blue-300 mt-2">
                  💡 {t("mcpConfigHint")}
                </p>
              </div>
            </div>
          </details>

          {/* SDK & Integration Links */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <a
              href="https://github.com/kagura-ai/kagura-memory-python-sdk"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
            >
              <span className="text-lg">🐍</span>
              <div>
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {t("sdkLinks.pythonSdk")}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  pip install kagura-memory
                </p>
              </div>
            </a>
            <a
              href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"}/redoc`}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
            >
              <span className="text-lg">📘</span>
              <div>
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {t("sdkLinks.restApi")}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  ReDoc / OpenAPI
                </p>
              </div>
            </a>
            <a
              href="https://github.com/kagura-ai/memory-cloud#claude-code-recommended"
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
            >
              <span className="text-lg">🤖</span>
              <div>
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  {t("sdkLinks.claudeCode")}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  .mcp.json
                </p>
              </div>
            </a>
          </div>

          {/* Create API Key Button */}
          <div>
            <ActionButton
              onClick={() => setShowCreateKeyDialog(true)}
              icon={<Plus className="w-4 h-4" />}
              variant="primary"
            >
              {t("createApiKey")}
            </ActionButton>
          </div>

          {/* API Keys Display */}
          {apiKeys.length === 0 ? (
            <div className="bg-gray-50 dark:bg-gray-800 p-6 rounded border border-gray-200 dark:border-gray-700 text-center">
              <p className="text-gray-500 dark:text-gray-400">
                {t("noApiKeys")}
              </p>
            </div>
          ) : (
            apiKeys.map((apiKey) => (
              <div
                key={apiKey.id}
                className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4 space-y-3"
              >
                {/* Key Name */}
                <div className="flex items-center justify-between">
                  <h4 className="font-semibold text-gray-900 dark:text-gray-100">
                    {apiKey.name}
                  </h4>
                  {apiKey.revoked_at && (
                    <span className="text-xs text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/20 px-2 py-1 rounded">
                      {t("revoked")}
                    </span>
                  )}
                </div>

                {/* Key Display */}
                {apiKey.is_visible && apiKey.plaintext_key ? (
                  <div className="bg-green-50 dark:bg-green-900/20 p-3 rounded border border-green-200 dark:border-green-800">
                    <div className="flex items-center gap-2">
                      <code className="flex-1 bg-white dark:bg-gray-900 px-3 py-2 rounded border border-gray-300 dark:border-gray-600 text-sm font-mono">
                        {apiKey.plaintext_key}
                      </code>
                      <button
                        onClick={() =>
                          handleCopy(
                            apiKey.plaintext_key!,
                            `api-key-${apiKey.id}`,
                          )
                        }
                        className="p-2 text-gray-600 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
                        title={t("copyToClipboard")}
                      >
                        {copiedItems[`api-key-${apiKey.id}`] ? (
                          <Check className="w-4 h-4 text-green-600" />
                        ) : (
                          <Copy className="w-4 h-4" />
                        )}
                      </button>
                      <button
                        onClick={() => handleHideAPIKeyClick(apiKey.id)}
                        className="p-2 text-gray-600 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
                        title={t("hideSecretNow")}
                      >
                        <EyeOff className="w-4 h-4" />
                      </button>
                      {apiKey.visibility_expires_at &&
                        (() => {
                          const expiresAt = new Date(
                            apiKey.visibility_expires_at,
                          );
                          const now = new Date();
                          const daysUntil =
                            (expiresAt.getTime() - now.getTime()) /
                            (1000 * 60 * 60 * 24);
                          if (daysUntil <= 0 || daysUntil > 30) return null;
                          return (
                            <span className="text-xs text-yellow-600 dark:text-yellow-400 flex items-center gap-1 whitespace-nowrap">
                              <AlertTriangle className="w-3 h-3" />
                              {t("hideInTime", {
                                time: formatRelativeTime(
                                  apiKey.visibility_expires_at,
                                  user?.timezone,
                                  locale,
                                ),
                              })}
                            </span>
                          );
                        })()}
                    </div>
                  </div>
                ) : (
                  <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700">
                    <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
                      <EyeOff className="w-4 h-4" />
                      <span className="text-sm">{t("apiKeyHidden")}</span>
                    </div>
                    {apiKey.key_prefix && (
                      <div className="mt-2 text-xs text-gray-400">
                        {t("prefix")}:{" "}
                        <code className="bg-gray-100 dark:bg-gray-900 px-1 py-0.5 rounded">
                          {apiKey.key_prefix}
                        </code>
                      </div>
                    )}
                  </div>
                )}

                {/* Action Buttons */}
                <div className="flex gap-2">
                  <ActionButton
                    onClick={() => handleRegenerateAPIKeyClick(apiKey.id)}
                    icon={<RefreshCw className="w-4 h-4" />}
                  >
                    {t("regenerate")}
                  </ActionButton>

                  {!apiKey.revoked_at && (
                    <ActionButton
                      onClick={() => handleDeleteAPIKeyClick(apiKey.id)}
                      variant="danger"
                      icon={<Trash2 className="w-4 h-4" />}
                    >
                      {tCommon("delete")}
                    </ActionButton>
                  )}
                </div>

                {/* Metadata */}
                <div className="text-xs text-gray-500 dark:text-gray-400 space-y-1">
                  <p>
                    {t("created")}:{" "}
                    {formatDateTime(apiKey.created_at, user?.timezone)}
                  </p>
                  {apiKey.revoked_at && (
                    <p className="text-red-600 dark:text-red-400">
                      {t("revoked")}:{" "}
                      {formatDateTime(apiKey.revoked_at, user?.timezone)}
                    </p>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
      </Section>

      {/* Create API Key Dialog */}
      <AlertDialog
        open={showCreateKeyDialog}
        onOpenChange={setShowCreateKeyDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("createApiKeyTitle")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium mb-1">
                    {t("keyName")}
                  </label>
                  <input
                    type="text"
                    value={newKeyName}
                    onChange={(e) => setNewKeyName(e.target.value)}
                    placeholder={t("keyNamePlaceholder")}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                  />
                </div>
                {createKeyError && (
                  <p className="text-sm text-red-600 dark:text-red-400">
                    {createKeyError}
                  </p>
                )}
                <p className="text-xs text-yellow-700 dark:text-yellow-300">
                  💡 {t("securityNote")}
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              onClick={() => {
                setShowCreateKeyDialog(false);
                setNewKeyName("");
                setCreateKeyError(null);
              }}
            >
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction onClick={handleCreateAPIKey}>
              {t("createKey")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Hide API Key Dialog */}
      <AlertDialog
        open={showHideApiKeyDialog}
        onOpenChange={setShowHideApiKeyDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("hideApiKeyTitle")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>{t("hideApiKeyWarning")}</p>
                <p>{t("hideApiKeyNote")}</p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmHideAPIKey}>
              {t("hideApiKey")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Regenerate API Key Dialog */}
      <AlertDialog
        open={showRegenerateApiKeyDialog}
        onOpenChange={setShowRegenerateApiKeyDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("regenerateApiKeyTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("regenerateApiKeyDesc")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={regenerating}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmRegenerateAPIKey}
              disabled={regenerating}
            >
              {regenerating ? tCommon("saving") : t("regenerate")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete API Key Dialog */}
      <AlertDialog
        open={showDeleteApiKeyDialog}
        onOpenChange={setShowDeleteApiKeyDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteApiKeyTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("deleteApiKeyDesc")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDeleteAPIKey}
              disabled={deleting}
              className="bg-red-600 hover:bg-red-700"
            >
              {deleting ? tCommon("saving") : tCommon("delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
