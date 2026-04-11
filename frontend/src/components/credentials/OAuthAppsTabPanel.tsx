/**
 * OAuthAppsTabPanel
 *
 * Self-contained panel for the OAuth Apps tab on the consolidated credentials page.
 * Contains all state, handlers, dialogs, and rendering from the oauth-apps page,
 * excluding PageContainer, PageHeader, and FeatureGuide.
 */

"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useTranslations, useLocale } from "next-intl";
import { Section } from "@/components/common/Section";
import { ActionButton } from "@/components/common/ActionButton";
import { LoadingState } from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useAuth } from "@/contexts/AuthContext";
import {
  getOAuth2Clients,
  createOAuth2Client,
  deleteOAuth2Client,
  regenerateOAuth2ClientSecret,
  OAuth2Client,
} from "@/lib/api/oauth";
import { EditOAuthClientDialog } from "@/app/(authenticated)/workspace/integrations/oauth-apps/EditOAuthClientDialog";
import { OAuthAppCard } from "@/components/oauth/OAuthAppCard";
import { CreateCustomOAuthAppDialog } from "@/components/oauth/CreateCustomOAuthAppDialog";
import { hideOAuthClientSecret } from "@/lib/api/member-credentials";
import { Copy, Check, Plus, KeyRound } from "lucide-react";
import { EmptyState } from "@/components/ui/empty-state";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
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
import { Button } from "@/components/ui/button";

// Auto-refresh interval: 5 minutes (refresh before 10-minute visibility expiry)
const OAUTH_REFRESH_INTERVAL_MS = 5 * 60 * 1000;

export function OAuthAppsTabPanel() {
  const t = useTranslations("customApps");
  const tCommon = useTranslations("common");
  const locale = useLocale();

  const { currentWorkspaceId } = useWorkspace();
  const { user } = useAuth();
  const { toast } = useToast();

  // URLs
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
  const baseUrl = apiUrl.replace(/\/api\/v1$/, "");
  const mcpBaseUrl = baseUrl + "/mcp";
  const workspaceScopedMcpUrl = currentWorkspaceId
    ? `${baseUrl}/mcp/w/${currentWorkspaceId}`
    : null;

  const [oauthClients, setOauthClients] = useState<OAuth2Client[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Per-key copy feedback (extracted into useCopyFeedback to fix the
  // multi-target stale-state bug from the pre-batch single-shared-ref pattern).
  const { isCopied, copyToTarget } = useCopyFeedback();

  // Custom OAuth App dialog
  const [showCustomDialog, setShowCustomDialog] = useState(false);

  // Confirmation dialog states
  const [showHideOAuthDialog, setShowHideOAuthDialog] = useState(false);
  const [oauthToHide, setOauthToHide] = useState<string | null>(null);
  const [showRegenerateOAuthDialog, setShowRegenerateOAuthDialog] =
    useState(false);
  const [oauthToRegenerate, setOauthToRegenerate] = useState<{
    clientId: string;
    provider: string;
  } | null>(null);
  const [showDeleteOAuthDialog, setShowDeleteOAuthDialog] = useState(false);
  const [oauthToDelete, setOauthToDelete] = useState<string | null>(null);

  // Edit dialog state
  const [showEditDialog, setShowEditDialog] = useState(false);
  const [oauthToEdit, setOauthToEdit] = useState<OAuth2Client | null>(null);

  // Track if component is mounted (used by load handlers; copy feedback
  // owns its own mount tracking inside the hook).
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
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
    } catch (err: unknown) {
      if (process.env.NODE_ENV === "development") {
        // eslint-disable-next-line no-console
        console.error("Failed to load OAuth clients:", err);
      }
      if (isMountedRef.current) {
        setError(err instanceof Error ? err.message : String(err));
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
      await copyToTarget(text, key);
    } catch (err: unknown) {
      // Clipboard write failure is a user-action failure — surface via
      // destructive toast per the 3-channel error rule.
      toast({
        title: tCommon("error"),
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
    }
  };

  // --- CRUD handlers ---

  const handleCreateOAuthApp = async (
    provider: "claude" | "chatgpt" | "custom",
  ) => {
    if (provider === "custom") {
      setShowCustomDialog(true);
      return;
    }

    try {
      await createOAuth2Client({
        provider,
        client_name: provider === "claude" ? "Claude" : "ChatGPT",
        redirect_uris:
          provider === "claude"
            ? ["https://claude.ai/api/mcp/auth_callback"]
            : ["https://chatgpt.com/connector/oauth/*"],
      });

      await loadOAuthClients();

      toast({
        title: tCommon("success"),
        description: t("createSuccess", {
          provider: provider === "claude" ? "Claude" : "ChatGPT",
        }),
      });
    } catch (err: unknown) {
      toast({
        title: tCommon("error"),
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
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
    } catch (err: unknown) {
      toast({
        title: tCommon("error"),
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
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
        title: tCommon("success"),
        description: t("regenerateSecretSuccess"),
      });
    } catch (err: unknown) {
      toast({
        title: tCommon("error"),
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
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
        title: tCommon("success"),
        description: t("deleteSuccess"),
      });
    } catch (err: unknown) {
      toast({
        title: tCommon("error"),
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
    }
  };

  const handleEdit = (app: OAuth2Client) => {
    setOauthToEdit(app);
    setShowEditDialog(true);
  };

  // --- Derived data ---

  if (loading && oauthClients.length === 0) {
    return <LoadingState lines={3} />;
  }

  const claudeApp = oauthClients.find((c) => c.provider === "claude");
  const chatgptApp = oauthClients.find((c) => c.provider === "chatgpt");
  const customApps = oauthClients.filter((c) => c.provider === "custom");

  const cardProps = {
    onCopy: handleCopy,
    isCopied,
    onHide: handleHideOAuthAppClick,
    onRegenerate: handleRegenerateOAuthClick,
    onDelete: handleDeleteOAuthClientClick,
    onEdit: handleEdit,
    timezone: user?.timezone,
    locale,
  };

  return (
    <>
      <ErrorBanner error={error} />

      {/* MCP Connection URL */}
      <Section
        title={`🔗 ${t("mcpConnection", { default: "MCP Connection" })}`}
        description={t("mcpConnectionDesc", {
          default: "MCP endpoint URL for all clients",
        })}
      >
        <div className="space-y-3">
          {workspaceScopedMcpUrl ? (
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-blue-50 dark:bg-blue-900/30 px-4 py-3 rounded border border-blue-200 dark:border-blue-800 text-sm font-mono text-blue-800 dark:text-blue-200">
                {workspaceScopedMcpUrl}
              </code>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() =>
                  handleCopy(workspaceScopedMcpUrl, "workspace-mcp-url")
                }
                className="text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-800"
                title={t("copyMcpUrl", { default: "Copy MCP URL" })}
                aria-label={t("copyMcpUrl", { default: "Copy MCP URL" })}
              >
                {isCopied("workspace-mcp-url") ? (
                  <Check className="w-4 h-4 text-green-600" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <code className="flex-1 bg-blue-50 dark:bg-blue-900/30 px-4 py-3 rounded border border-blue-200 dark:border-blue-800 text-sm font-mono text-blue-800 dark:text-blue-200">
                {mcpBaseUrl}
              </code>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => handleCopy(mcpBaseUrl, "mcp-url")}
                className="text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-800"
                title={t("copyMcpUrl", { default: "Copy MCP URL" })}
                aria-label={t("copyMcpUrl", { default: "Copy MCP URL" })}
              >
                {isCopied("mcp-url") ? (
                  <Check className="w-4 h-4 text-green-600" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </Button>
            </div>
          )}
        </div>
      </Section>

      {/* OAuth Applications */}
      <Section>
        <div className="space-y-6">
          {/* Claude & ChatGPT Apps */}
          {(
            [
              {
                provider: "claude" as const,
                app: claudeApp,
                icon: "🧠",
                title: t("claude"),
                subtitle: t("claudeSubtitle"),
              },
              {
                provider: "chatgpt" as const,
                app: chatgptApp,
                icon: "🤖",
                title: t("chatgpt"),
                subtitle: t("chatgptSubtitle"),
              },
            ] as const
          ).map(({ provider, app, icon, title, subtitle }) => (
            <div
              key={provider}
              className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-4"
            >
              <div className="flex items-center gap-2 mb-3">
                <span className="text-2xl">{icon}</span>
                <div>
                  <h3 className="font-semibold text-gray-900 dark:text-gray-100">
                    {title}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {subtitle}
                  </p>
                </div>
              </div>

              {app ? (
                <OAuthAppCard app={app} copyKey={provider} {...cardProps} />
              ) : (
                <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700">
                  <p className="text-sm text-gray-500 dark:text-gray-400 mb-3">
                    {t("noOAuthApp", { provider: title })}
                  </p>
                  <ActionButton
                    onClick={() => handleCreateOAuthApp(provider)}
                    icon={<Plus className="w-4 h-4" />}
                  >
                    {t("createOAuthApp", { provider: title })}
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
                  <h3 className="font-semibold text-gray-900 dark:text-gray-100">
                    {t("customOAuthApps")}
                  </h3>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    {t("customOAuthAppsDesc")}
                  </p>
                </div>
              </div>
              <ActionButton
                onClick={() => handleCreateOAuthApp("custom")}
                icon={<Plus className="w-4 h-4" />}
                variant="primary"
              >
                {t("createCustomApp")}
              </ActionButton>
            </div>

            {customApps.length === 0 ? (
              <EmptyState
                icon={KeyRound}
                title={t("noCustomOAuthAppsTitle")}
                description={t("noCustomApps")}
                actionLabel={t("createCustomApp")}
                onAction={() => handleCreateOAuthApp("custom")}
              />
            ) : (
              <div className="space-y-4">
                {customApps.map((app) => (
                  <div
                    key={app.client_id}
                    className="border-t border-gray-200 dark:border-gray-700 pt-4 first:border-t-0 first:pt-0"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <h4 className="font-semibold text-gray-900 dark:text-gray-100">
                        {app.client_name}
                      </h4>
                    </div>
                    <OAuthAppCard
                      app={app}
                      copyKey={`custom-${app.client_id}`}
                      {...cardProps}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </Section>

      {/* Create Custom OAuth App Dialog */}
      <CreateCustomOAuthAppDialog
        isOpen={showCustomDialog}
        onOpenChange={setShowCustomDialog}
        onSuccess={() => {
          setShowCustomDialog(false);
          loadOAuthClients();
        }}
      />

      {/* Hide OAuth Secret Dialog */}
      <AlertDialog
        open={showHideOAuthDialog}
        onOpenChange={setShowHideOAuthDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("hideOAuthTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("hideOAuthWarning")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <p className="text-sm text-muted-foreground">{t("hideOAuthNote")}</p>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmHideOAuthApp}>
              {t("hideSecret")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Regenerate OAuth Secret Dialog */}
      <AlertDialog
        open={showRegenerateOAuthDialog}
        onOpenChange={setShowRegenerateOAuthDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("regenerateOAuthTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("regenerateOAuthDesc", {
                provider: oauthToRegenerate?.provider || "",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmRegenerateOAuth}>
              {t("regenerate")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete OAuth App Dialog */}
      <AlertDialog
        open={showDeleteOAuthDialog}
        onOpenChange={setShowDeleteOAuthDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteOAuthTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("deleteOAuthDesc")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDeleteOAuthClient}
              className="bg-red-600 hover:bg-red-700"
            >
              {tCommon("delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Edit OAuth Client Dialog */}
      <EditOAuthClientDialog
        client={oauthToEdit}
        open={showEditDialog}
        onOpenChange={setShowEditDialog}
        onSuccess={loadOAuthClients}
      />
    </>
  );
}
