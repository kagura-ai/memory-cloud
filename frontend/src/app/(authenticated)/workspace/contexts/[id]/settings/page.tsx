"use client";

/**
 * Context Settings Page
 *
 * Issue #169: Context-specific settings page
 * Issue #96: Unified from edit modal — single source for all context editing
 *
 * UI pattern: Card-based layout matching search-settings page
 */

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { PageContainer } from "@/components/common/PageContainer";
import { PageHeader } from "@/components/common/PageHeader";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
import { Label } from "@/components/ui/label";
import {
  ArrowLeft,
  Save,
  Trash2,
  AlertCircle,
  Loader2,
  Brain,
  Lock,
  ShieldCheck,
  ShieldOff,
  Copy,
  Info,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { getContext, updateContext, deleteContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
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
  const { currentWorkspace } = useWorkspace();
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
  const [isPrivate, setIsPrivate] = useState(true);
  const [isPublic, setIsPublic] = useState(false);
  const [isLocked, setIsLocked] = useState(false);
  const [resourceId, setResourceId] = useState("");

  const [idCopied, setIdCopied] = useState(false);
  const [lockSaving, setLockSaving] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [privacyDialogOpen, setPrivacyDialogOpen] = useState(false);
  const [pendingPrivacyChange, setPendingPrivacyChange] = useState<
    boolean | null
  >(null);

  const applyContextData = useCallback((data: Context) => {
    setContext(data);
    setDisplayName(data.display_name || "");
    setDescription(data.description || "");
    setSummary(data.summary || "");
    setUsageGuide(data.usage_guide || "");
    setIsPrivate(data.is_private ?? true);
    setIsPublic(data.is_public ?? false);
    setIsLocked(data.is_locked ?? false);
  }, []);

  const fetchContext = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await getContext(contextId);
      applyContextData(data);
    } catch {
      setError("Failed to load context");
    } finally {
      setLoading(false);
    }
  }, [contextId, applyContextData]);

  const refreshContext = useCallback(async () => {
    try {
      const data = await getContext(contextId);
      applyContextData(data);
    } catch {
      // Silent refresh — don't show error
    }
  }, [contextId, applyContextData]);

  useEffect(() => {
    fetchContext();
  }, [fetchContext]);

  const handleSave = async () => {
    if (!context) return;

    if (isPublic && !context.is_public && !resourceId.trim()) {
      toast({
        title: "Resource ID required",
        description: "Enter a Resource ID before making this context public.",
        variant: "destructive",
      });
      return;
    }

    try {
      setSaving(true);
      setError(null);

      let resource_id: string | undefined = undefined;
      if (isPublic && !context.is_public) {
        resource_id = resourceId.trim();
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
      await refreshContext();
    } catch (err: unknown) {
      const apiError = err as { message?: string };
      toast({
        title: "Save failed",
        description: apiError?.message || "Failed to save settings",
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
      const apiError = err as { details?: { detail?: string } };
      setError(apiError?.details?.detail || "Failed to delete context");
      setDeleteDialogOpen(false);
    } finally {
      setDeleting(false);
    }
  };

  const handleLockToggle = async () => {
    if (!context) return;
    const newLocked = !isLocked;
    try {
      setLockSaving(true);
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

  const handlePrivacyToggle = (newIsPrivate: boolean) => {
    if (!context?.is_private && newIsPrivate) {
      setPendingPrivacyChange(newIsPrivate);
      setPrivacyDialogOpen(true);
    } else {
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
            The context you&apos;re looking for doesn&apos;t exist or you
            don&apos;t have access.
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

  const isOwner = currentWorkspace?.current_user_role === "owner";

  const isDirty =
    displayName !== (context.display_name || "") ||
    description !== (context.description || "") ||
    summary !== (context.summary || "") ||
    usageGuide !== (context.usage_guide || "") ||
    isPrivate !== (context.is_private ?? true) ||
    isPublic !== (context.is_public ?? false);

  return (
    <PageContainer>
      <PageHeader
        title={`${context.display_name || context.name} — Settings`}
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
              Back
            </Button>
          </div>
        }
      />

      {error && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Basic Information */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Info className="h-5 w-5" />
            Basic Information
          </CardTitle>
          <CardDescription>
            Core context details visible in the dashboard
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label>Context ID</Label>
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
            <p className="text-sm text-muted-foreground">
              Use this ID in MCP tools (e.g.,{" "}
              <code className="text-xs">context_id</code> parameter).
            </p>
          </div>

          <div className="space-y-2">
            <Label>Context Name</Label>
            <Input value={context.name} disabled className="font-mono" />
            <p className="text-sm text-muted-foreground">
              Internal identifier. Cannot be changed after creation.
            </p>
          </div>

          <div className="space-y-2">
            <Label>Display Name</Label>
            <Input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={context.name}
              maxLength={200}
            />
            <p className="text-sm text-muted-foreground">
              Human-readable name shown in the dashboard. Leave empty to use the
              context name.
            </p>
          </div>

          <div className="space-y-2">
            <Label>Description</Label>
            <Textarea
              placeholder="What is this context for?"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
            />
          </div>
        </CardContent>
      </Card>

      {/* AI Configuration */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Brain className="h-5 w-5" />
            AI Configuration
          </CardTitle>
          <CardDescription>
            Settings that help AI understand and use this context
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label>Template (optional)</Label>
            <Select
              onValueChange={(templateId) => {
                const template = getTemplate(templateId);
                if (template) {
                  setSummary(template.summary);
                  setUsageGuide(template.usage_guide);
                }
              }}
            >
              <SelectTrigger>
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
            <p className="text-sm text-muted-foreground">
              Or customize the fields below manually.
            </p>
          </div>

          <div className="space-y-2">
            <Label>Summary (for AI)</Label>
            <Textarea
              placeholder="Brief description of this context's purpose (200-500 chars)"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              maxLength={500}
              rows={3}
            />
            <p className="text-sm text-muted-foreground">
              Helps AI understand the purpose of this context. {summary.length}
              /500
            </p>
          </div>

          <div className="space-y-2">
            <Label>Usage Guide (for AI)</Label>
            <Textarea
              placeholder="Guidelines for how AI should use memories in this context..."
              value={usageGuide}
              onChange={(e) => setUsageGuide(e.target.value)}
              maxLength={2000}
              rows={6}
            />
            <p className="text-sm text-muted-foreground">
              Instructions for AI on how to store and retrieve memories.{" "}
              {usageGuide.length}/2000
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Privacy & Access Control */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Lock className="h-5 w-5" />
            Privacy & Access Control
          </CardTitle>
          <CardDescription>Control who can access this context</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Private/Shared Toggle */}
          <div className="flex items-center justify-between p-4 border rounded-lg">
            <div className="flex-1">
              <p className="font-medium text-sm">
                {isPrivate ? "🔒 Private Context" : "👥 Shared Context"}
              </p>
              <p className="text-sm text-muted-foreground">
                {isPrivate
                  ? "Only you can access this context"
                  : "Workspace members can be added to access this context"}
              </p>
            </div>
            <Button
              variant={isPrivate ? "outline" : "default"}
              size="sm"
              onClick={() => handlePrivacyToggle(!isPrivate)}
            >
              {isPrivate ? "Make Shared" : "Make Private"}
            </Button>
          </div>

          {/* Public Access (Owner only) */}
          {isOwner &&
            (isPrivate ? (
              <Alert>
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>
                  Make this context Shared first to enable Public access.
                </AlertDescription>
              </Alert>
            ) : isPublic || context.is_public ? (
              <div className="space-y-3">
                <div className="flex items-center justify-between p-4 border rounded-lg">
                  <div className="flex-1">
                    <p className="font-medium text-sm">🌍 Public Context</p>
                    <p className="text-sm text-muted-foreground">
                      External systems can search via Public REST API
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

                {!isPublic && context.is_public && (
                  <Alert>
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>
                      Unpublish pending — click Save Changes to apply.
                    </AlertDescription>
                  </Alert>
                )}

                {isPublic && context.resource_id && (
                  <Alert>
                    <AlertCircle className="h-4 w-4" />
                    <AlertTitle>Public Search API</AlertTitle>
                    <AlertDescription className="space-y-2">
                      <div>
                        <p className="text-xs font-medium mb-1">Resource ID:</p>
                        <code className="block p-2 bg-muted rounded text-xs font-mono break-all">
                          {context.resource_id}
                        </code>
                      </div>
                      <div>
                        <p className="text-xs font-medium mb-1">Endpoint:</p>
                        <code className="block p-2 bg-muted rounded text-xs font-mono break-all">
                          POST /api/v1/public/{contextId}/search
                        </code>
                      </div>
                      <div className="flex gap-3 mt-2 text-xs">
                        <a
                          href="/workspace/integrations/resource-tokens"
                          className="text-primary underline hover:text-primary/80"
                        >
                          Manage Resource Tokens →
                        </a>
                        <a
                          href="https://github.com/kagura-ai/kagura-memory-python-sdk"
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-primary underline hover:text-primary/80"
                        >
                          Python SDK →
                        </a>
                      </div>
                    </AlertDescription>
                  </Alert>
                )}
              </div>
            ) : (
              <div className="space-y-3">
                <div className="space-y-2">
                  <Label>
                    Resource ID <span className="text-red-500">*</span>
                  </Label>
                  <Input
                    placeholder="e.g., products, docs_articles"
                    value={resourceId}
                    onChange={(e) => {
                      const value = e.target.value
                        .toLowerCase()
                        .replace(/[^a-z0-9_]/g, "");
                      setResourceId(value);
                    }}
                    className="font-mono text-sm"
                  />
                  <p className="text-sm text-muted-foreground">
                    Lowercase letters, numbers, underscores only. Used as the
                    resource ID for Public Search API.
                  </p>
                </div>
                <Button
                  variant="outline"
                  className="w-full"
                  disabled={!resourceId.trim()}
                  onClick={() => {
                    if (resourceId.trim()) setIsPublic(true);
                  }}
                >
                  🌍 Make Public
                </Button>
              </div>
            ))}
        </CardContent>
      </Card>

      {/* Protection & Danger Zone */}
      {!context.is_default && (
        <Card className="border-red-200 dark:border-red-900">
          <CardHeader>
            <CardTitle className="text-red-900 dark:text-red-400">
              Protection & Danger Zone
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between p-4 border rounded-lg">
              <div className="flex-1">
                <p className="font-medium text-sm flex items-center gap-2">
                  {isLocked ? (
                    <ShieldCheck className="h-4 w-4 text-amber-600" />
                  ) : (
                    <ShieldOff className="h-4 w-4 text-muted-foreground" />
                  )}
                  {isLocked ? "Context Locked" : "Context Unlocked"}
                </p>
                <p className="text-sm text-muted-foreground">
                  {isLocked
                    ? "This context is protected from accidental deletion"
                    : "Lock to prevent accidental deletion"}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={handleLockToggle}
                disabled={lockSaving}
              >
                {lockSaving ? "Saving..." : isLocked ? "Unlock" : "Lock"}
              </Button>
            </div>

            <div
              className={`p-4 border border-red-200 dark:border-red-900 rounded-lg ${isLocked ? "opacity-50" : ""}`}
            >
              <p className="font-semibold text-red-900 dark:text-red-400 mb-2">
                Delete Context
              </p>
              <p className="text-sm text-muted-foreground mb-4">
                {isLocked
                  ? `Cannot delete "${context.name}" — context is locked.`
                  : `Permanently delete "${context.name}" and all memories. This cannot be undone.`}
              </p>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => setDeleteDialogOpen(true)}
                disabled={isLocked}
              >
                <Trash2 className="h-4 w-4 mr-2" />
                Delete Context
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete Context</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to delete &quot;{context.name}&quot;? This
              will permanently delete all memories in this context. This action
              cannot be undone.
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

      {/* Privacy Change Confirmation Dialog */}
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
                  This action will immediately revoke access for all other
                  members.
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

      {/* Sticky Save Bar — visible only when there are unsaved changes */}
      {isDirty && (
        <div className="fixed bottom-0 left-0 right-0 z-50 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="container flex items-center justify-between py-3 px-4 max-w-4xl mx-auto">
            <p className="text-sm text-muted-foreground">
              You have unsaved changes
            </p>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={refreshContext}>
                Discard
              </Button>
              <Button size="sm" onClick={handleSave} disabled={saving}>
                {saving ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Save className="h-4 w-4 mr-2" />
                )}
                {saving ? "Saving..." : "Save Changes"}
              </Button>
            </div>
          </div>
        </div>
      )}
    </PageContainer>
  );
}
