/**
 * Resource Tokens Tab Panel
 *
 * Extracted from the resource-tokens page for use in the consolidated
 * credentials page. Contains all logic and rendering except
 * PageContainer, PageHeader, and FeatureGuide wrappers.
 */

"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { Section } from "@/components/common/Section";
import {
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import {
  listResourceTokens,
  revokeResourceToken,
  updateResourceToken,
  type ResourceToken,
} from "@/lib/api/resource-tokens";
import { getContexts } from "@/lib/api/contexts";
import { ApiError } from "@/lib/api/base";
import { getMaxQuotaCapacity } from "@/config/resource-tokens";
import { Plus, AlertTriangle, ChevronDown } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
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
import { ResourceTokensTable } from "@/components/resource-tokens/ResourceTokensTable";
import { CreateResourceTokenDialog } from "@/components/resource-tokens/CreateResourceTokenDialog";

interface ResourceTokensTabPanelProps {
  /**
   * Pre-filter the token list to a single resource. When provided, takes
   * precedence over the `?resource_id=` URL query — used by the per-resource
   * Tokens tab on `/workspace/resources/[id]` where the slug is in the path,
   * not the query string.
   */
  resourceIdFilter?: string;
}

export function ResourceTokensTabPanel({
  resourceIdFilter: resourceIdFilterProp,
}: ResourceTokensTabPanelProps = {}) {
  const t = useTranslations("resourceTokens");
  const tCommon = useTranslations("common");
  const { currentWorkspaceId, currentWorkspace } = useWorkspace();
  const { toast } = useToast();

  const [tokens, setTokens] = useState<ResourceToken[]>([]);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [contexts, setContexts] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Pagination state
  const [totalTokens, setTotalTokens] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [limit] = useState(50);

  // Dialog states
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [showRevokeDialog, setShowRevokeDialog] = useState(false);
  const [showEditDialog, setShowEditDialog] = useState(false);
  const [tokenToRevoke, setTokenToRevoke] = useState<ResourceToken | null>(
    null,
  );
  const [tokenToEdit, setTokenToEdit] = useState<ResourceToken | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editDescriptionInput, setEditDescriptionInput] = useState("");
  const [editQuotaInput, setEditQuotaInput] = useState("");

  // Revoked tokens visibility toggle
  const [showRevoked, setShowRevoked] = useState(false);

  // Check if user is owner
  const isOwner = currentWorkspace?.current_user_role === "owner";

  // Issue #47: Deep-link support — `?resource_id=<id>` pre-filters the token list.
  // Passed through to the backend list endpoint; also gates the create dialog's
  // initial resource selection.
  // Issue #325: a `resourceIdFilter` prop wins over the URL query so the panel
  // works inside the per-resource Tokens tab where the slug is a path segment.
  const searchParams = useSearchParams();
  const resourceIdFilter =
    resourceIdFilterProp || searchParams.get("resource_id") || undefined;

  // Track the previous filter so we can detect an actual change and reset
  // pagination within the same effect as the load. A separate reset effect
  // would race with the load effect in the same commit and cause a duplicate
  // fetch with the old offset before the reset takes effect.
  const prevFilterRef = useRef(resourceIdFilter);

  // Load tokens on mount and page change
  useEffect(() => {
    if (!currentWorkspaceId) return;
    if (!isOwner) return; // Skip loading if not owner

    // Filter change: reset pagination and skip this cycle's fetch —
    // the setCurrentPage(1) will re-trigger this effect with the right offset.
    if (prevFilterRef.current !== resourceIdFilter) {
      prevFilterRef.current = resourceIdFilter;
      if (currentPage !== 1) {
        setCurrentPage(1);
        return;
      }
    }
    loadTokens();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentWorkspaceId, currentPage, isOwner, resourceIdFilter]);

  const loadTokens = async () => {
    try {
      setLoading(true);
      setError(null);
      const offset = (currentPage - 1) * limit;
      const [tokensResponse, contextsData] = await Promise.all([
        listResourceTokens(resourceIdFilter, limit, offset),
        getContexts(),
      ]);
      // Handle paginated response
      setTokens(tokensResponse.tokens);
      setTotalTokens(tokensResponse.total);
      setContexts(contextsData.contexts);
    } catch (err: unknown) {
      if (process.env.NODE_ENV === "development") {
        console.error("Failed to load resource tokens:", err);
      }

      // Use structured error codes instead of string matching
      // Normal empty state: RES-001 (Resource not found) with 404 status
      const apiErr = err instanceof ApiError ? err : null;
      const isNormalEmptyState =
        apiErr?.error === "RES-001" && apiErr?.status === 404;

      if (!isNormalEmptyState) {
        // Load failure: ErrorBanner only — no toast (single channel per
        // error class, see .claude/rules/frontend.md > Error Surface).
        setError(t("loadError"));
      }
      // For empty/normal state, just set empty arrays (no error message)
      setTokens([]);
      setTotalTokens(0);
    } finally {
      setLoading(false);
    }
  };

  const handleEditClick = (token: ResourceToken) => {
    setTokenToEdit(token);
    setEditDescriptionInput(token.description || "");
    setEditQuotaInput(token.quota_events_per_hour.toString());
    setShowEditDialog(true);
  };

  const handleEditConfirm = async () => {
    if (!tokenToEdit) return;

    try {
      setEditing(true);
      await updateResourceToken(tokenToEdit.id, {
        description: editDescriptionInput.trim() || null,
        quota_events_per_hour: parseInt(editQuotaInput, 10),
      });

      toast({
        title: tCommon("success"),
        description: t("editDialog.updateSuccess"),
      });

      await loadTokens();
      setShowEditDialog(false);
      setTokenToEdit(null);
    } catch (err: unknown) {
      if (process.env.NODE_ENV === "development") {
        console.error("Failed to update token:", err);
      }
      toast({
        title: tCommon("error"),
        description: t("editDialog.updateError") || "Failed to update token",
        variant: "destructive",
      });
    } finally {
      setEditing(false);
    }
  };

  const handleRevokeClick = (token: ResourceToken) => {
    setTokenToRevoke(token);
    setShowRevokeDialog(true);
  };

  const handleRevokeConfirm = async () => {
    if (!tokenToRevoke) return;

    try {
      setRevoking(true);
      await revokeResourceToken(tokenToRevoke.id);

      toast({
        title: tCommon("success"),
        description: t("revokeSuccess", {
          resourceId: tokenToRevoke.resource_id,
        }),
      });

      // Reload tokens
      await loadTokens();

      // Close dialog
      setShowRevokeDialog(false);
      setTokenToRevoke(null);
    } catch (err: unknown) {
      if (process.env.NODE_ENV === "development") {
        console.error("Failed to revoke resource token:", err);
      }
      toast({
        title: tCommon("error"),
        description: t("revokeError"),
        variant: "destructive",
      });
    } finally {
      setRevoking(false);
    }
  };

  const handleCreateSuccess = async () => {
    await loadTokens();
  };

  if (loading && tokens.length === 0) {
    return <TableLoadingState rows={3} />;
  }

  return (
    <div>
      {/* Step Flow */}
      <div className="flex items-center justify-center gap-2 my-6 p-4 bg-gray-50 dark:bg-gray-800/50 rounded-lg">
        <Link
          href="/workspace/contexts"
          className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 hover:text-brand-green-600"
        >
          <span className="flex items-center justify-center w-6 h-6 rounded-full bg-gray-200 dark:bg-gray-700 text-xs font-bold">
            1
          </span>
          <span>{t("stepFlow.step1", { default: "Set Resource ID" })}</span>
        </Link>
        <span className="text-gray-400">→</span>
        <span className="flex items-center gap-2 text-sm font-medium text-brand-green-700 dark:text-brand-green-400">
          <span className="flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-100 dark:bg-brand-green-900 text-xs font-bold text-brand-green-700 dark:text-brand-green-300">
            2
          </span>
          <span>{t("stepFlow.step2", { default: "Create Token" })}</span>
        </span>
        <span className="text-gray-400">→</span>
        <span className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
          <span className="flex items-center justify-center w-6 h-6 rounded-full bg-gray-200 dark:bg-gray-700 text-xs font-bold">
            3
          </span>
          <span>{t("stepFlow.step3", { default: "Send Data" })}</span>
        </span>
      </div>

      {/* SDK Quick Link */}
      <div className="text-center text-xs text-gray-500 dark:text-gray-400 -mt-4 mb-4">
        {t("sdkHint", { default: "Or use the" })}{" "}
        <a
          href="https://github.com/kagura-ai/kagura-memory-python-sdk"
          target="_blank"
          rel="noopener noreferrer"
          className="text-brand-green-600 dark:text-brand-green-400 underline hover:text-brand-green-700"
        >
          Python SDK
        </a>{" "}
        {t("sdkHintSuffix", { default: "for programmatic resource ingestion" })}
      </div>

      {/* Resource Tokens Content */}
      <div className="mt-6">
        <ErrorBanner error={error} />

        {!isOwner && (
          <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 mb-6">
            <div className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-amber-600" />
              <p className="text-sm text-amber-800">{t("ownerOnly")}</p>
            </div>
          </div>
        )}

        {/* Prerequisites Warning (Unified) */}
        {isOwner &&
          (currentWorkspace?.plan_name === "free" ||
            !contexts.some((c) => c.resource_id)) && (
            <div className="rounded-lg border-2 border-purple-200 bg-purple-50 p-4 mb-6">
              <div className="flex items-start gap-3">
                <AlertTriangle className="h-5 w-5 text-purple-600 flex-shrink-0 mt-0.5" />
                <div className="flex-1">
                  <p className="text-sm font-medium text-purple-900">
                    {t("noResourceIdWarning")}
                  </p>
                  <p className="text-xs text-purple-700 mt-1 mb-3">
                    {t("noResourceIdWarningDesc")}
                  </p>
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-xs">
                      <span className="text-purple-900">1.</span>
                      <a
                        href="/workspace/contexts"
                        className="text-purple-600 hover:text-purple-700 underline font-medium"
                      >
                        {t("goToContexts")}
                      </a>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

        {/* Usage Guide */}
        {tokens.length > 0 && (
          <Section>
            <Collapsible className="border border-blue-200 dark:border-blue-800 rounded-lg">
              <CollapsibleTrigger className="group flex w-full items-center justify-between cursor-pointer px-4 py-3 bg-blue-50 dark:bg-blue-900/20 hover:bg-blue-100 dark:hover:bg-blue-900/30 rounded-lg font-medium text-blue-900 dark:text-blue-100 text-sm">
                <span>{t("guideTitle")}</span>
                <ChevronDown
                  className="w-4 h-4 transition-transform duration-200 group-data-[state=open]:rotate-180"
                  aria-hidden="true"
                />
              </CollapsibleTrigger>
              <CollapsibleContent className="p-4 bg-blue-50 dark:bg-blue-900/20 space-y-4">
                <div>
                  <p className="font-semibold text-gray-900 dark:text-gray-100 mb-2 text-sm">
                    {t("guide.dataManagementTitle")}
                  </p>
                  <p className="text-xs text-gray-700 dark:text-gray-300 mb-2">
                    {t("guide.dataManagementDesc")}
                  </p>
                  <div className="bg-white dark:bg-gray-800 border border-blue-200 dark:border-blue-700 rounded p-3 space-y-2">
                    <div className="space-y-1 text-xs">
                      <p className="text-green-700 dark:text-green-300">
                        {t("guide.dataFeature1")}
                      </p>
                      <p className="text-green-700 dark:text-green-300">
                        {t("guide.dataFeature2")}
                      </p>
                      <p className="text-green-700 dark:text-green-300">
                        {t("guide.dataFeature3")}
                      </p>
                      <p className="text-green-700 dark:text-green-300">
                        {t("guide.dataFeature4")}
                      </p>
                    </div>
                    <div className="border-t border-blue-200 dark:border-blue-700 pt-2 space-y-1 text-xs">
                      <p className="text-amber-700 dark:text-amber-300">
                        {t("guide.dataLimitation1")}
                      </p>
                      <p className="text-amber-700 dark:text-amber-300">
                        {t("guide.dataLimitation2")}
                      </p>
                      <p className="text-amber-700 dark:text-amber-300">
                        {t("guide.dataLimitation3")}
                      </p>
                    </div>
                    <p className="text-xs text-blue-700 dark:text-blue-300 italic border-t border-blue-200 dark:border-blue-700 pt-2">
                      {t("guide.dataNote")}
                    </p>
                  </div>
                </div>

                <div className="border-t border-blue-200 dark:border-blue-700 pt-4">
                  <p className="font-semibold text-gray-900 dark:text-gray-100 mb-2 text-sm">
                    {t("guide.n1DesignTitle")}
                  </p>
                  <div className="bg-white dark:bg-gray-800 border border-blue-200 dark:border-blue-700 rounded p-3 space-y-2">
                    <ul className="text-xs text-gray-700 dark:text-gray-300 space-y-1.5">
                      <li className="flex items-start gap-2">
                        <span className="text-blue-600 dark:text-blue-400">
                          •
                        </span>
                        <span>
                          <strong>{t("guide.n1Rule1")}</strong>
                        </span>
                      </li>
                      <li className="flex items-start gap-2">
                        <span className="text-blue-600 dark:text-blue-400">
                          •
                        </span>
                        <span>
                          <strong>{t("guide.n1Rule2")}</strong>
                        </span>
                      </li>
                    </ul>
                    <div className="mt-3 p-2 bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-700 rounded">
                      <p className="text-xs font-medium text-blue-900 dark:text-blue-100 mb-1.5">
                        {t("guide.exampleTitle", { resourceId: "products" })}
                      </p>
                      <div className="text-xs text-blue-800 dark:text-blue-200 space-y-1 ml-2">
                        <p>{t("guide.tokenA")}</p>
                        <p>{t("guide.tokenB")}</p>
                        <p>{t("guide.tokenC")}</p>
                        <p className="mt-2 pt-2 border-t border-blue-200 dark:border-blue-700 text-blue-700 dark:text-blue-300">
                          {t("guide.result1", { resourceId: "products" })}
                          <br />
                          {t("guide.result2")}
                        </p>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="border-t border-blue-200 dark:border-blue-700 pt-4">
                  <p className="font-semibold text-gray-900 dark:text-gray-100 mb-2 text-sm">
                    {t("guide.usageTitle")}
                  </p>
                  <div className="space-y-3">
                    <div>
                      <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">
                        <strong>{t("guide.step1Title")}</strong>
                      </p>
                      <p className="text-xs text-gray-600 dark:text-gray-400">
                        {t("guide.step1Desc")}
                      </p>
                    </div>

                    <div>
                      <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">
                        <strong>{t("guide.step2Title")}</strong>
                      </p>
                      <pre className="bg-gray-900 text-gray-100 p-3 rounded text-xs overflow-x-auto">
                        {`curl -X POST "http://localhost:8080/api/v1/resources/{resource_id}/events" \\
  -H "X-Resource-API-Key: YOUR_TOKEN_HERE" \\
  -H "Content-Type: application/json" \\
  -d '{
  "op": "upsert",
  "doc_id": "DOC-001",
  "version": 1,
  "payload": {
    "title": "Sample Document",
    "price": 1000
  }
}'`}
                      </pre>
                      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-blue-700 dark:text-blue-300">
                        <span>
                          <code className="bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">
                            op
                          </code>
                          : {t("guide.paramOp")}
                        </span>
                        <span>
                          <code className="bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">
                            doc_id
                          </code>
                          : {t("guide.paramDocId")}
                        </span>
                        <span>
                          <code className="bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">
                            version
                          </code>
                          : {t("guide.paramVersion")}
                        </span>
                        <span>
                          <code className="bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 rounded">
                            payload
                          </code>
                          : {t("guide.paramPayload")}
                        </span>
                      </div>
                      <div className="mt-2 pt-2 border-t border-blue-200 dark:border-blue-700 space-y-1">
                        <p className="text-xs text-blue-700 dark:text-blue-300">
                          💡 {t("guide.upsertNote")}
                        </p>
                        <p className="text-xs text-amber-700 dark:text-amber-300">
                          ⚠️ {t("guide.deleteNote")}
                        </p>
                      </div>
                    </div>

                    <div>
                      <p className="text-sm text-gray-700 dark:text-gray-300 mb-2">
                        <strong>{t("guide.step3Title")}</strong>
                      </p>
                      <p className="text-xs text-gray-600 dark:text-gray-400">
                        {t("guide.step3Desc")}
                      </p>
                      <a
                        href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080"}/redoc#tag/resource-ingest`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sm text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300 underline inline-block mt-2"
                      >
                        {t("guide.viewApiDocs")}
                      </a>
                    </div>
                  </div>
                </div>
              </CollapsibleContent>
            </Collapsible>
          </Section>
        )}

        <Section>
          {/* Token Summary */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            <div className="p-4 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-lg">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-slate-600 dark:text-slate-400">
                    {t("activeTokens")}
                  </p>
                  <p className="text-3xl font-bold text-slate-900 dark:text-white mt-1">
                    {tokens.filter((tk) => tk.status === "active").length}
                    <span className="text-lg text-slate-400">
                      {" "}
                      /{" "}
                      {currentWorkspace?.plan_name === "pro"
                        ? 30
                        : currentWorkspace?.plan_name === "basic"
                          ? 3
                          : 0}
                    </span>
                  </p>
                </div>
                {tokens.filter((tk) => tk.status === "revoked").length > 0 && (
                  <div className="text-right">
                    <p className="text-xs text-slate-500">
                      {t("revokedCount", {
                        count: tokens.filter((tk) => tk.status === "revoked")
                          .length,
                      })}
                    </p>
                  </div>
                )}
              </div>
            </div>

            <div className="p-4 bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-lg">
              <p className="text-sm text-slate-600 dark:text-slate-400">
                {t("quotaUsage")}
              </p>
              <p className="text-3xl font-bold text-slate-900 dark:text-white mt-1">
                {tokens
                  .filter((tk) => tk.status === "active")
                  .reduce((sum, tk) => sum + tk.quota_events_per_hour, 0)
                  .toLocaleString()}
                <span className="text-sm text-slate-400 ml-2">
                  {t("eventsPerHour")}
                </span>
              </p>
              <p className="text-xs text-slate-500 mt-1">
                {(() => {
                  const planName = (currentWorkspace?.plan_name || "free") as
                    | "free"
                    | "basic"
                    | "pro";
                  const maxQuota = getMaxQuotaCapacity(planName);
                  const currentQuota = tokens
                    .filter((tk) => tk.status === "active")
                    .reduce((sum, tk) => sum + tk.quota_events_per_hour, 0);
                  const percentage =
                    maxQuota > 0
                      ? ((currentQuota / maxQuota) * 100).toFixed(1)
                      : 0;
                  return t("maxCapacity", {
                    percentage,
                    max: maxQuota.toLocaleString(),
                  });
                })()}
              </p>
            </div>
          </div>

          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center gap-2">
              <label
                htmlFor="show-revoked-tokens"
                className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-400 cursor-pointer"
              >
                <Checkbox
                  id="show-revoked-tokens"
                  checked={showRevoked}
                  onCheckedChange={(checked) =>
                    setShowRevoked(checked === true)
                  }
                />
                {t("showRevokedTokens")}
              </label>
            </div>
            {isOwner && (
              <Button
                onClick={() => setShowCreateDialog(true)}
                disabled={
                  !contexts.some((c) => c.resource_id) ||
                  currentWorkspace?.plan_name === "free"
                }
              >
                <Plus className="h-4 w-4 mr-2" />
                {t("createToken")}
              </Button>
            )}
          </div>

          <ResourceTokensTable
            tokens={
              showRevoked
                ? tokens
                : tokens.filter((tk) => tk.status === "active")
            }
            contexts={contexts}
            loading={loading}
            onRevoke={handleRevokeClick}
            onEdit={isOwner ? handleEditClick : undefined}
          />

          {/* Pagination Controls */}
          {totalTokens > limit && (
            <div className="mt-6 flex items-center justify-between border-t pt-4">
              <div className="text-sm text-muted-foreground">
                {t("paginationInfo", {
                  start: (currentPage - 1) * limit + 1,
                  end: Math.min(currentPage * limit, totalTokens),
                  total: totalTokens,
                })}
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                  disabled={currentPage === 1 || loading}
                >
                  {tCommon("previous")}
                </Button>
                <span className="text-sm px-3">
                  {t("pageInfo", {
                    current: currentPage,
                    total: Math.ceil(totalTokens / limit),
                  })}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCurrentPage((p) => p + 1)}
                  disabled={
                    currentPage >= Math.ceil(totalTokens / limit) || loading
                  }
                >
                  {tCommon("next")}
                </Button>
              </div>
            </div>
          )}
        </Section>

        {/* Create Token Dialog */}
        {isOwner && (
          <CreateResourceTokenDialog
            isOpen={showCreateDialog}
            onClose={() => setShowCreateDialog(false)}
            onSuccess={handleCreateSuccess}
            currentTokens={tokens}
            initialResourceId={resourceIdFilter}
          />
        )}

        {/* Edit Token Dialog */}
        {tokenToEdit && (
          <AlertDialog open={showEditDialog} onOpenChange={setShowEditDialog}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>{t("editDialog.title")}</AlertDialogTitle>
                <AlertDialogDescription>
                  {t("editDialog.description", {
                    resourceId: tokenToEdit.resource_id,
                  })}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <div className="space-y-4 py-4">
                <div className="space-y-2">
                  <Label>{t("editDialog.descriptionLabel")}</Label>
                  <Textarea
                    value={editDescriptionInput}
                    onChange={(e) => setEditDescriptionInput(e.target.value)}
                    rows={2}
                    placeholder={t("createDialog.descriptionPlaceholder")}
                  />
                </div>
                <div className="space-y-2">
                  <Label>{t("editDialog.quotaLabel")}</Label>
                  <Input
                    type="number"
                    min="1"
                    max={(() => {
                      const maxPerToken = 10000;
                      const maxTokens =
                        currentWorkspace?.plan_name === "pro"
                          ? 30
                          : currentWorkspace?.plan_name === "basic"
                            ? 3
                            : 0;
                      const maxTotalQuota = maxTokens * maxPerToken;
                      const usedByOthers = tokens
                        .filter(
                          (tk) =>
                            tk.status === "active" && tk.id !== tokenToEdit.id,
                        )
                        .reduce((sum, tk) => sum + tk.quota_events_per_hour, 0);
                      return Math.min(
                        maxTotalQuota - usedByOthers,
                        maxPerToken,
                      );
                    })()}
                    value={editQuotaInput}
                    onChange={(e) => setEditQuotaInput(e.target.value)}
                  />
                  <p className="text-xs text-slate-500">
                    {(() => {
                      const planName = (currentWorkspace?.plan_name ||
                        "free") as "free" | "basic" | "pro";
                      const maxTotalQuota = getMaxQuotaCapacity(planName);
                      const usedByOthers = tokens
                        .filter(
                          (tk) =>
                            tk.status === "active" && tk.id !== tokenToEdit.id,
                        )
                        .reduce((sum, tk) => sum + tk.quota_events_per_hour, 0);
                      const available = maxTotalQuota - usedByOthers;
                      return t("editDialog.availableQuota", {
                        available: available.toLocaleString(),
                        unit: t("eventsPerHour"),
                      });
                    })()}
                  </p>
                </div>
              </div>
              <AlertDialogFooter>
                <AlertDialogCancel disabled={editing}>
                  {t("editDialog.cancel")}
                </AlertDialogCancel>
                <AlertDialogAction
                  onClick={handleEditConfirm}
                  disabled={editing}
                >
                  {editing && <InlineSpinner size="sm" className="mr-2" />}
                  {editing ? t("editDialog.updating") : t("editDialog.update")}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        )}

        {/* Revoke Confirmation Dialog */}
        <AlertDialog open={showRevokeDialog} onOpenChange={setShowRevokeDialog}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{t("revokeDialog.title")}</AlertDialogTitle>
              <AlertDialogDescription>
                {t("revokeDialog.description")}{" "}
                <strong className="font-semibold">
                  {tokenToRevoke?.resource_id}
                </strong>
                ? {t("revokeDialog.warning")}
              </AlertDialogDescription>
            </AlertDialogHeader>
            {tokenToRevoke && (
              <div className="p-2 bg-blue-50 dark:bg-blue-900/20 rounded text-sm mt-2 mb-2">
                <p className="text-blue-900 dark:text-blue-100">
                  💡{" "}
                  {t("revokeDialog.quotaFreed", {
                    quota: tokenToRevoke.quota_events_per_hour.toLocaleString(),
                  })}
                </p>
              </div>
            )}
            <AlertDialogFooter>
              <AlertDialogCancel disabled={revoking}>
                {t("revokeDialog.cancel")}
              </AlertDialogCancel>
              <AlertDialogAction
                onClick={handleRevokeConfirm}
                disabled={revoking}
                className="bg-red-600 hover:bg-red-700"
              >
                {revoking
                  ? t("revokeDialog.revoking")
                  : t("revokeDialog.revoke")}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  );
}
