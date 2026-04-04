"use client";

/**
 * Context Settings Page
 *
 * Issue #169: Context-specific settings page
 * - Basic info (name, description, summary, usage_guide)
 * - Search settings link
 * - Danger zone (delete)
 */

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { Section } from "@/components/common/Section";
import { ActionButton } from "@/components/common/ActionButton";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import {
  ArrowLeft,
  Save,
  Trash2,
  AlertCircle,
  Loader2,
  Settings,
  Search,
  ExternalLink,
  ShieldCheck,
  ShieldOff,
  Copy,
} from "lucide-react";
import { cn, typography, colors } from "@/styles/design-tokens";
import { useToast } from "@/hooks/use-toast";
import { getContext, updateContext, deleteContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { useAuth } from "@/contexts/AuthContext";
import { useSearchParams } from "next/navigation";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CONTEXT_TEMPLATES, getTemplate } from "@/lib/templates/usage-guide";

export default function ContextSettingsPage() {
  const params = useParams();
  const router = useRouter();
  const { refetchUser, user } = useAuth();
  const { toast } = useToast();
  const searchParams = useSearchParams();
  const contextId = params.id as string;
  const contextIdFromUrl = searchParams?.get("context");

  const [context, setContext] = useState<Context | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [summary, setSummary] = useState("");
  const [usageGuide, setUsageGuide] = useState("");
  const [isPrivate, setIsPrivate] = useState(true); // Migration 034
  const [isPublic, setIsPublic] = useState(false); // Issue #238: Public context
  const [isLocked, setIsLocked] = useState(false); // Issue #85: Context lock
  const [resourceIdPrefix, setResourceIdPrefix] = useState(""); // Issue #238: Resource ID prefix

  // Copy feedback
  const [idCopied, setIdCopied] = useState(false);

  // Lock toggle (Issue #85)
  const [lockSaving, setLockSaving] = useState(false);

  // Delete dialog
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Privacy confirmation dialog (Migration 034)
  const [privacyDialogOpen, setPrivacyDialogOpen] = useState(false);
  const [pendingPrivacyChange, setPendingPrivacyChange] = useState<
    boolean | null
  >(null);

  const fetchContext = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await getContext(contextId);
      setContext(data);
      setDisplayName(data.display_name || "");
      setDescription(data.description || "");
      setSummary(data.summary || "");
      setUsageGuide(data.usage_guide || "");
      setIsPrivate(data.is_private ?? true); // Migration 034
      setIsPublic(data.is_public ?? false); // Issue #238
      setIsLocked(data.is_locked ?? false); // Issue #85
    } catch (err) {
      console.error("Failed to fetch context:", err);
      setError("Failed to load context");
    } finally {
      setLoading(false);
    }
  }, [contextId]);

  useEffect(() => {
    fetchContext();
  }, [fetchContext]);

  const handleSave = async () => {
    if (!context) return;

    // Validate: resource_id prefix required when making public
    if (isPublic && !context.is_public && !resourceIdPrefix.trim()) {
      toast({
        title: "Resource ID required",
        description:
          "Enter a Resource ID Prefix before making this context public.",
        variant: "destructive",
      });
      return;
    }

    try {
      setSaving(true);
      setError(null);

      // Generate resource_id if making public for the first time
      let resource_id: string | undefined = undefined;
      if (isPublic && !context.is_public) {
        resource_id = resourceIdPrefix.trim();
      }

      await updateContext(context.id, {
        display_name: displayName.trim() || undefined,
        description: description.trim() || undefined,
        summary: summary.trim() || undefined,
        usage_guide: usageGuide.trim() || undefined,
        is_private: isPrivate,
        is_public: isPublic,
        resource_id: resource_id,
      });

      toast({
        title: "Settings saved",
        description: "Context settings have been updated.",
      });

      fetchContext();
    } catch (err: unknown) {
      const apiError = err as { message?: string };
      const errorMsg = apiError?.message || "Failed to save settings";
      toast({
        title: "Save failed",
        description: errorMsg,
        variant: "destructive",
        duration: 6000,
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!context) return;

    try {
      setDeleting(true);
      await deleteContext(context.id);
      await refetchUser();
      router.push("/workspace/contexts");
    } catch (err: unknown) {
      console.error("Failed to delete context:", err);
      const apiError = err as { details?: { detail?: string } };
      setError(apiError?.details?.detail || "Failed to delete context");
      setDeleteDialogOpen(false);
    } finally {
      setDeleting(false);
    }
  };

  // Issue #85: Handle lock toggle
  const handleLockToggle = async () => {
    if (!context) return;
    const newLocked = !isLocked;
    try {
      setLockSaving(true);
      setError(null);
      await updateContext(context.id, { is_locked: newLocked });
      setIsLocked(newLocked);
      toast({
        title: newLocked ? "Context locked" : "Context unlocked",
        description: newLocked
          ? "This context is now protected from deletion"
          : "This context can now be deleted",
      });
    } catch {
      toast({
        title: "Failed to update lock status",
        variant: "destructive",
      });
    } finally {
      setLockSaving(false);
    }
  };

  // Migration 034: Handle privacy toggle with confirmation
  const handlePrivacyToggle = (newIsPrivate: boolean) => {
    // Shared → Private: Show confirmation (will remove members)
    if (!context?.is_private && newIsPrivate) {
      setPendingPrivacyChange(newIsPrivate);
      setPrivacyDialogOpen(true);
    } else {
      // Private → Shared: Just update
      setIsPrivate(newIsPrivate);
    }
  };

  const confirmPrivacyChange = () => {
    if (pendingPrivacyChange !== null) {
      setIsPrivate(pendingPrivacyChange);
    }
    setPrivacyDialogOpen(false);
    setPendingPrivacyChange(null);
  };

  if (loading) {
    return (
      <PageContainer>
        <SpinnerLoading size="lg" message="Loading context settings..." />
      </PageContainer>
    );
  }

  if (!context) {
    return (
      <PageContainer>
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Context Not Found</AlertTitle>
          <AlertDescription>
            The context you're looking for doesn't exist or you don't have
            access.
          </AlertDescription>
        </Alert>
        <Button
          variant="outline"
          onClick={() => router.push("/workspace/contexts")}
          className="mt-4"
        >
          <ArrowLeft className="h-4 w-4 mr-2" />
          Back to Contexts
        </Button>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={`${context.name} - Settings`}
        description="Manage context settings and configurations"
        actions={
          <div className="flex items-center gap-2">
            {(contextId === user?.current_context_id ||
              contextId === contextIdFromUrl) && (
              <Badge variant="default" className="bg-brand-green-600">
                Current
              </Badge>
            )}
            {context.is_default && <Badge variant="secondary">Default</Badge>}
            <Button
              variant="outline"
              onClick={() => router.push("/workspace/contexts")}
            >
              <ArrowLeft className="h-4 w-4 mr-2" />
              Back to Contexts
            </Button>
          </div>
        }
      />

      {error && (
        <Alert variant="destructive" className="mb-6">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Error</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Basic Info Section */}
      <Section
        title="Basic Information"
        description="Core context details visible in the dashboard"
      >
        <div className="space-y-4 max-w-2xl">
          <div className="space-y-2">
            <label className={cn(typography.bodySmall, "font-medium")}>
              Context ID
            </label>
            <div className="flex items-center gap-2">
              <Input
                value={context.id}
                disabled
                className="font-mono text-xs"
              />
              <Button
                variant="outline"
                size="sm"
                className="shrink-0"
                onClick={() => {
                  navigator.clipboard.writeText(context.id);
                  setIdCopied(true);
                  setTimeout(() => setIdCopied(false), 1500);
                }}
              >
                {idCopied ? (
                  <span className="text-xs text-green-600">Copied!</span>
                ) : (
                  <Copy className="h-4 w-4" />
                )}
              </Button>
            </div>
            <p className={cn(typography.caption, colors.text.muted)}>
              Use this ID in MCP tools (e.g.,{" "}
              <code className="text-xs">context_id</code> parameter).
            </p>
          </div>

          <div className="space-y-2">
            <label className={cn(typography.bodySmall, "font-medium")}>
              Context Name
            </label>
            <Input value={context.name} disabled className="font-mono" />
            <p className={cn(typography.caption, colors.text.muted)}>
              Internal identifier. Cannot be changed after creation.
            </p>
          </div>

          <div className="space-y-2">
            <label className={cn(typography.bodySmall, "font-medium")}>
              Display Name
            </label>
            <Input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={context.name}
              maxLength={200}
            />
            <p className={cn(typography.caption, colors.text.muted)}>
              Human-readable name shown in the dashboard. Leave empty to use the
              context name.
            </p>
          </div>

          <div className="space-y-2">
            <label className={cn(typography.bodySmall, "font-medium")}>
              Description
            </label>
            <Textarea
              placeholder="What is this context for?"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
            />
          </div>
        </div>
      </Section>

      {/* AI Configuration Section */}
      <Section
        title="AI Configuration"
        description="Settings that help AI understand and use this context"
      >
        <div className="space-y-4 max-w-2xl">
          {/* Template Selector */}
          <div className="space-y-2">
            <label className={cn(typography.bodySmall, "font-medium")}>
              Template (optional)
            </label>
            <Select
              onValueChange={(templateId) => {
                const template = getTemplate(templateId);
                if (template) {
                  setSummary(template.summary);
                  setUsageGuide(template.usage_guide);
                }
              }}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Choose a template to auto-fill Summary and Usage Guide..." />
              </SelectTrigger>
              <SelectContent>
                {CONTEXT_TEMPLATES.map((t) => (
                  <SelectItem key={t.id} value={t.id}>
                    <div className="flex items-center gap-2">
                      <span className="text-xs px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800">
                        {t.category}
                      </span>
                      <span className="font-medium">{t.name}</span>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className={cn(typography.caption, colors.text.muted)}>
              Or customize the fields below manually.
            </p>
          </div>

          <div className="space-y-2">
            <label className={cn(typography.bodySmall, "font-medium")}>
              Summary (for AI)
            </label>
            <Textarea
              placeholder="Brief description of this context's purpose (200-500 chars)"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              maxLength={500}
              rows={3}
            />
            <p className={cn(typography.caption, colors.text.muted)}>
              Helps AI understand the purpose of this context. {summary.length}
              /500
            </p>
          </div>

          <div className="space-y-2">
            <label className={cn(typography.bodySmall, "font-medium")}>
              Usage Guide (for AI)
            </label>
            <Textarea
              placeholder="Guidelines for how AI should use memories in this context..."
              value={usageGuide}
              onChange={(e) => setUsageGuide(e.target.value)}
              maxLength={2000}
              rows={6}
            />
            <p className={cn(typography.caption, colors.text.muted)}>
              Instructions for AI on how to store and retrieve memories.{" "}
              {usageGuide.length}/2000
            </p>
          </div>
        </div>
      </Section>

      {/* Search & Retrieval Section */}
      <Section
        title="Search & Retrieval"
        description="Configure how memories are searched and ranked in this context"
      >
        <div className="space-y-4 max-w-2xl">
          <div className="p-4 border rounded-lg bg-slate-50 dark:bg-slate-900/50">
            <div className="flex items-start gap-4">
              <div className="p-2 bg-blue-100 dark:bg-blue-900/30 rounded-lg">
                <Search className="h-5 w-5 text-blue-600" />
              </div>
              <div className="flex-1">
                <h4 className={cn(typography.bodySmall, "font-semibold mb-1")}>
                  Search Settings
                </h4>
                <p
                  className={cn(typography.caption, colors.text.muted, "mb-3")}
                >
                  Configure hybrid search weights, fetch factor, and reranking
                  options.
                </p>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    router.push(
                      `/workspace/contexts/${contextId}/search-settings`,
                    )
                  }
                >
                  <Settings className="h-4 w-4 mr-2" />
                  Open Search Settings
                  <ExternalLink className="h-3 w-3 ml-2" />
                </Button>
              </div>
            </div>
          </div>
        </div>
      </Section>

      {/* Privacy & Access Control - Migration 034 + Issue #238 */}
      <Section
        title="Privacy & Access Control"
        description="Control who can access this context"
      >
        <div className="space-y-4 max-w-2xl">
          {/* Private/Shared Toggle */}
          <div className="flex items-center justify-between p-4 border rounded-lg">
            <div className="flex-1">
              <h4 className="font-medium text-sm mb-1">
                {isPrivate ? "🔒 Private Context" : "👥 Shared Context"}
              </h4>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {isPrivate
                  ? "Only you can access this context"
                  : "Workspace members can be added to access this context"}
              </p>
            </div>
            <button
              onClick={() => handlePrivacyToggle(!isPrivate)}
              className={cn(
                "px-4 py-2 rounded font-medium text-sm transition-colors",
                isPrivate
                  ? "bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300"
                  : "bg-blue-600 text-white hover:bg-blue-700",
              )}
            >
              {isPrivate ? "Make Shared" : "Make Private"}
            </button>
          </div>

          {!isPrivate && (
            <Alert>
              <AlertCircle className="h-4 w-4" />
              <AlertTitle>Shared Context</AlertTitle>
              <AlertDescription>
                This context is shared. You can manage members from the{" "}
                <a
                  href={`/contexts/${contextId}/members`}
                  className="text-blue-600 hover:underline"
                >
                  Members page
                </a>
                .
              </AlertDescription>
            </Alert>
          )}

          {/* Public Access - Issue #238 */}
          {isPrivate ? (
            <Alert className="border-yellow-200 bg-yellow-50 dark:bg-yellow-900/20">
              <AlertCircle className="h-4 w-4 text-yellow-600" />
              <AlertTitle className="text-yellow-800 dark:text-yellow-200">
                Private Context Cannot Be Public
              </AlertTitle>
              <AlertDescription className="text-yellow-700 dark:text-yellow-300">
                Make this context Shared first to enable Public access.
              </AlertDescription>
            </Alert>
          ) : isPublic || context?.is_public ? (
            /* Already public or pending public */
            <div className="space-y-3">
              <div className="flex items-center justify-between p-4 border rounded-lg bg-gradient-to-r from-purple-50 to-blue-50 dark:from-purple-950/20 dark:to-blue-950/20">
                <div className="flex-1">
                  <h4 className="font-medium text-sm mb-1">
                    ��� Public Context
                  </h4>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    External systems can search this context via Public REST API
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="border-red-300 text-red-700 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-950"
                  onClick={() => setIsPublic(false)}
                >
                  Unpublish
                </Button>
              </div>

              {!isPublic && context?.is_public && (
                <Alert className="border-orange-200 bg-orange-50 dark:bg-orange-900/20">
                  <AlertCircle className="h-4 w-4 text-orange-600" />
                  <AlertDescription className="text-orange-700 dark:text-orange-300">
                    Unpublish pending — click Save Changes to apply.
                  </AlertDescription>
                </Alert>
              )}

              {isPublic && context?.resource_id && (
                <Alert className="border-purple-200 bg-purple-50 dark:bg-purple-900/20">
                  <AlertCircle className="h-4 w-4 text-purple-600" />
                  <AlertTitle className="text-purple-800 dark:text-purple-200">
                    Public Search API
                  </AlertTitle>
                  <AlertDescription className="text-purple-700 dark:text-purple-300 space-y-2">
                    <div className="mb-2">
                      <p className="text-xs font-medium mb-1">Resource ID:</p>
                      <code className="block p-2 bg-purple-100 dark:bg-purple-900/40 rounded text-xs font-mono break-all">
                        {context.resource_id}
                      </code>
                    </div>
                    <p>External systems can search this context using:</p>
                    <code className="block mt-2 p-2 bg-purple-100 dark:bg-purple-900/40 rounded text-xs font-mono break-all">
                      POST /api/v1/public/{contextId}/search
                    </code>
                  </AlertDescription>
                </Alert>
              )}
            </div>
          ) : (
            /* Not yet public — show prefix input + make public button */
            <div className="space-y-3">
              <div className="p-4 border-2 rounded-lg bg-blue-50 dark:bg-blue-950/20 border-blue-200 dark:border-blue-700">
                <label
                  className={cn(
                    typography.bodySmall,
                    "font-semibold mb-2 block",
                  )}
                >
                  Resource ID Prefix <span className="text-red-500">*</span>
                </label>
                <Input
                  placeholder="e.g., products, docs_articles"
                  value={resourceIdPrefix}
                  onChange={(e) => {
                    const value = e.target.value
                      .toLowerCase()
                      .replace(/[^a-z0-9_]/g, "");
                    setResourceIdPrefix(value);
                  }}
                  className="font-mono text-sm"
                />
                <p
                  className={cn(typography.caption, colors.text.muted, "mt-2")}
                >
                  Lowercase letters, numbers, underscores only. Used as the
                  resource ID for Public Search API.
                </p>
              </div>
              <button
                onClick={() => {
                  if (resourceIdPrefix.trim()) {
                    setIsPublic(true);
                  }
                }}
                disabled={!resourceIdPrefix.trim()}
                className={cn(
                  "w-full px-4 py-3 rounded-lg border-2 border-dashed transition-colors",
                  !resourceIdPrefix.trim()
                    ? "border-gray-300 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/20 opacity-50 cursor-not-allowed"
                    : "border-purple-300 dark:border-purple-700 bg-purple-50 dark:bg-purple-950/20 hover:bg-purple-100 dark:hover:bg-purple-900/30 cursor-pointer",
                )}
              >
                <div
                  className={cn(
                    "text-sm font-medium",
                    !resourceIdPrefix.trim()
                      ? "text-gray-500"
                      : "text-purple-700 dark:text-purple-300",
                  )}
                >
                  🌍 Make Public
                </div>
                <div
                  className={cn(
                    "text-xs mt-1",
                    !resourceIdPrefix.trim()
                      ? "text-gray-400"
                      : "text-purple-600 dark:text-purple-400",
                  )}
                >
                  {!resourceIdPrefix.trim()
                    ? "Enter Resource ID Prefix first"
                    : "Enable Public Search API for this context"}
                </div>
              </button>
            </div>
          )}
        </div>
      </Section>

      {/* Save All Changes — covers Basic Info, AI Config, Privacy & Public */}
      <div className="flex items-center justify-between max-w-2xl p-4 border rounded-lg bg-slate-50 dark:bg-slate-900/50">
        <p className={cn(typography.caption, colors.text.muted)}>
          Saves display name, description, AI settings, and privacy/public
          changes.
        </p>
        <ActionButton
          onClick={handleSave}
          icon={
            saving ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Save className="h-4 w-4" />
            )
          }
          variant="primary"
          disabled={saving}
        >
          {saving ? "Saving..." : "Save All Changes"}
        </ActionButton>
      </div>

      {/* Protection & Danger Zone */}
      {!context.is_default && (
        <Section
          title="Protection & Danger Zone"
          className="border-red-200 dark:border-red-900"
        >
          <div className="space-y-4 max-w-2xl">
            {/* Issue #85: Context Lock Toggle */}
            <div className="flex items-center justify-between p-4 border rounded-lg">
              <div className="flex-1">
                <h4 className="font-medium text-sm mb-1 flex items-center gap-2">
                  {isLocked ? (
                    <ShieldCheck className="h-4 w-4 text-amber-600" />
                  ) : (
                    <ShieldOff className="h-4 w-4 text-gray-400" />
                  )}
                  {isLocked ? "Context Locked" : "Context Unlocked"}
                </h4>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  {isLocked
                    ? "This context is protected from accidental deletion"
                    : "This context can be deleted — lock it to prevent accidental deletion"}
                </p>
              </div>
              <button
                onClick={handleLockToggle}
                disabled={lockSaving}
                className={cn(
                  "px-4 py-2 rounded font-medium text-sm transition-colors",
                  isLocked
                    ? "bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300 hover:bg-amber-200"
                    : "bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300",
                )}
              >
                {lockSaving ? "Saving..." : isLocked ? "Unlock" : "Lock"}
              </button>
            </div>

            <div
              className={cn(
                "p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900 rounded",
                isLocked && "opacity-50",
              )}
            >
              <h3 className="font-semibold text-red-900 dark:text-red-400 mb-2">
                Delete Context
              </h3>
              <p className="text-sm text-red-700 dark:text-red-300 mb-4">
                {isLocked
                  ? `Cannot delete "${context.name}" — context is locked. Unlock it first.`
                  : `This will permanently delete "${context.name}" and all memories in this context. This action cannot be undone.`}
              </p>
              <ActionButton
                onClick={() => setDeleteDialogOpen(true)}
                icon={<Trash2 className="h-4 w-4" />}
                variant="danger"
                disabled={isLocked}
              >
                Delete Context
              </ActionButton>
            </div>
          </div>
        </Section>
      )}

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Context</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete "{context.name}"? This will
              permanently delete all memories in this context's collection. This
              action cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-red-600 hover:bg-red-700"
              disabled={deleting}
            >
              {deleting && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
              Delete Context
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Privacy Change Confirmation Dialog - Migration 034 */}
      <AlertDialog open={privacyDialogOpen} onOpenChange={setPrivacyDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Make Context Private?</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm text-muted-foreground">
                <div>
                  Changing this context to <strong>Private</strong> will remove
                  all members except you (the owner).
                </div>
                <div className="text-yellow-600 dark:text-yellow-400 font-medium">
                  ⚠️ This action will immediately revoke access for all other
                  members.
                </div>
                <div className="text-sm text-gray-600 dark:text-gray-400">
                  This context will also be removed from any member&apos;s
                  allowed context list.
                </div>
                <div>
                  You can re-add members later by changing back to Shared.
                </div>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              onClick={() => {
                setPrivacyDialogOpen(false);
                setPendingPrivacyChange(null);
              }}
            >
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmPrivacyChange}
              className="bg-yellow-600 hover:bg-yellow-700"
            >
              Make Private
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
