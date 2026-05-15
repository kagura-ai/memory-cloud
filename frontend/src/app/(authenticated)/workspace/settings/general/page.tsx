"use client";

/**
 * Workspace Settings Page
 *
 * Issue #115 Phase B-5: Workspace-level Multi-tenancy Frontend
 * Issue #212: Simplified to edit-only mode (workspace auto-created on login)
 * Issue #223: i18n support
 * Issue #276: Added create mode (?create=true) for new workspace creation
 *
 * Edit workspace details (name, description) or create new workspace.
 */

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import { ActionButton } from "@/components/common/ActionButton";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useAuth } from "@/contexts/AuthContext";
import {
  getWorkspace,
  updateWorkspace,
  deleteWorkspace,
  Workspace,
} from "@/lib/api/workspaces";
import { Save, Trash2 } from "lucide-react";
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
import { WorkspaceCreateForm } from "@/components/workspaces/WorkspaceCreateForm";
import { ApiError } from "@/lib/api/base";
import { writeRecentlyDeletedWorkspace } from "@/lib/storage/recently-deleted-workspace";

export default function WorkspaceSettingsPage() {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const router = useRouter();
  const searchParams = useSearchParams();
  const {
    currentWorkspaceId,
    currentWorkspace,
    refreshWorkspaces,
    workspaces,
    loading: workspaceLoading,
  } = useWorkspace();
  const { toast } = useToast();
  const { logout, user } = useAuth();

  // Issue #276: Check if in create mode
  const isCreateMode = searchParams.get("create") === "true";

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  // Delete dialog state
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  useEffect(() => {
    // Issue #276: Skip loading for create mode
    if (isCreateMode) {
      setLoading(false);
      return;
    }

    // Wait for workspace context to finish loading
    if (workspaceLoading) return;

    // Owner-only access check
    if (currentWorkspace && currentWorkspace.current_user_role !== "owner") {
      router.push("/workspace/dashboard");
      return;
    }

    if (currentWorkspaceId) {
      loadWorkspace();
    }
  }, [currentWorkspaceId, currentWorkspace, workspaceLoading, isCreateMode]);

  const loadWorkspace = async () => {
    if (!currentWorkspaceId) return;

    try {
      setLoading(true);
      const workspace = await getWorkspace(currentWorkspaceId);
      setWorkspace(workspace);
      setName(workspace.name);
      setDescription(workspace.description || "");
    } catch (error) {
      console.error("Failed to load workspace:", error);
      setError(t("failedToLoadWorkspace"));
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    if (!currentWorkspaceId) return;

    try {
      setSaving(true);
      setError(null);

      // Validation
      if (!name) {
        setError(t("workspaceNameRequired"));
        return;
      }

      // Update workspace
      await updateWorkspace(currentWorkspaceId, {
        name,
        description: description || undefined,
      });

      toast({
        title: tCommon("success"),
        description: t("settingsSaved"),
      });
      await loadWorkspace();
      await refreshWorkspaces();
    } catch (err: unknown) {
      console.error("Failed to save workspace:", err);

      // Extract validation error details
      const apiError = err instanceof ApiError ? err : null;
      // FastAPI 422 detail can be a string or validation error array at runtime
      if (apiError?.status === 422 && apiError.details?.detail !== undefined) {
        const rawDetail = apiError.details["detail"] as unknown;
        if (Array.isArray(rawDetail)) {
          const messages = (
            rawDetail as Array<{ loc?: string[]; msg?: string }>
          )
            .map((e) => `${e.loc?.join(".") || "Field"}: ${e.msg}`)
            .join("\n");
          setError(messages);
        } else {
          setError(apiError.message || t("validationFailed"));
        }
      } else {
        setError(
          err instanceof Error ? err.message : t("failedToUpdateWorkspace"),
        );
      }
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteClick = () => {
    setShowDeleteDialog(true);
  };

  const handleConfirmDelete = async () => {
    if (!currentWorkspaceId) return;

    // Issue #218: Check if this is the last workspace before deletion
    const isLastWorkspace = workspaces.length === 1;

    setDeleting(true);
    // Capture pre-delete state for the post-success stash. `workspace.name` is
    // read here because `workspace` (and `user`) might be cleared during the
    // post-success refresh/redirect cycle.
    const deletedName = workspace?.name ?? null;
    const stashUserId = user?.id ?? null;
    try {
      await deleteWorkspace(currentWorkspaceId);

      // Issue #660: stash the deleted name so the dashboard can render an
      // auto-switch toast after the backend picks a remaining workspace.
      // Written ONLY after the API call succeeds — Copilot review on PR #662
      // caught that pre-write left a misleading stash on delete failure.
      // Skip on the last-workspace path (user is being logged out — no switch).
      if (!isLastWorkspace && deletedName && stashUserId) {
        writeRecentlyDeletedWorkspace({
          user_id: stashUserId,
          id: currentWorkspaceId,
          name: deletedName,
          ts: Date.now(),
        });
      }

      // Close dialog first
      setShowDeleteDialog(false);

      if (isLastWorkspace) {
        // Last workspace deleted - logout user
        toast({
          title: t("workspaceDeleted"),
          description: t("lastWorkspaceDeleteDesc"),
        });

        // Small delay to allow toast to show, then logout
        setTimeout(async () => {
          await logout();
        }, 1000);
      } else {
        // Still have workspaces - refresh and redirect to home
        toast({
          title: t("workspaceDeleted"),
          description: t("workspaceDeletedDesc"),
        });

        await refreshWorkspaces();

        setTimeout(() => {
          router.push("/");
          router.refresh();
        }, 500);
      }
    } catch (error: unknown) {
      console.error("Failed to delete workspace:", error);
      toast({
        title: tCommon("error"),
        description:
          error instanceof Error ? error.message : t("failedToDeleteWorkspace"),
        variant: "destructive",
      });
      setDeleting(false);
    }
  };

  // Issue #276: Show create form for create mode
  if (isCreateMode) {
    return (
      <PageContainer>
        <WorkspaceCreateForm
          onSuccess={(workspace) => {
            router.push("/workspace/dashboard");
          }}
          onCancel={() => router.back()}
        />
      </PageContainer>
    );
  }

  if (loading) {
    return (
      <PageContainer>
        <PageHeader title={t("workspaceSettings")} />
        <div className="flex items-center justify-center py-12">
          <SpinnerLoading message={tCommon("loading")} />
        </div>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={t("workspaceSettings")}
        description={t("settingsDesc")}
      />

      <Section>
        <div className="space-y-4 max-w-2xl">
          {/* Error Display */}
          {error && (
            <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900 rounded">
              <p className="text-sm text-red-700 dark:text-red-300 whitespace-pre-line">
                {error}
              </p>
            </div>
          )}

          {/* Workspace Name */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t("workspaceName")} *
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("workspaceNamePlaceholder")}
              required
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {t("workspaceNameHelp")}
            </p>
          </div>

          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t("workspaceDesc")} {t("optional")}
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("descPlaceholder")}
              rows={3}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {t("descHelp")}
            </p>
          </div>

          {/* Action Buttons */}
          <div className="flex gap-3 pt-4">
            <ActionButton
              onClick={handleSave}
              icon={<Save className="w-4 h-4" />}
              disabled={!name || saving}
            >
              {saving ? t("saving") : tCommon("saveChanges")}
            </ActionButton>

            <button
              onClick={() => router.back()}
              className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100"
            >
              {tCommon("cancel")}
            </button>
          </div>
        </div>
      </Section>

      {/* Danger Zone */}
      {workspace && (
        <Section
          title={t("dangerZone")}
          className="border-red-200 dark:border-red-900"
        >
          <div className="space-y-4 max-w-2xl">
            <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-900 rounded">
              <h3 className="font-semibold text-red-900 dark:text-red-400 mb-2">
                {t("deleteWorkspace")}
              </h3>
              <p className="text-sm text-red-700 dark:text-red-300 mb-4">
                {t("deleteWorkspaceWarning")}
              </p>
              <ActionButton
                onClick={handleDeleteClick}
                icon={<Trash2 className="w-4 h-4" />}
                variant="danger"
              >
                {t("deleteWorkspace")}
              </ActionButton>
            </div>
          </div>
        </Section>
      )}

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="text-red-600 dark:text-red-400">
              {t("deleteConfirmTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>
                  {t("deleteWorkspaceDialogDesc", {
                    workspaceName: workspace?.name || "",
                  })}
                </p>
                <ul className="list-disc list-inside space-y-1 text-sm">
                  <li>{t("allContexts")}</li>
                  <li>{t("allMemories")}</li>
                  <li>{t("allMembers")}</li>
                </ul>
                <p className="font-semibold text-red-600 dark:text-red-400">
                  {tCommon("thisActionCannotBeUndone")}
                </p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDelete}
              disabled={deleting}
              className="bg-red-600 hover:bg-red-700 text-white"
            >
              {deleting ? t("deleting") : t("deleteWorkspace")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}
