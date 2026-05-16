"use client";

/**
 * WorkspaceCreateForm Component
 *
 * Issue #276: Form for creating a new workspace.
 * Used in workspace/settings page when ?create=true is set.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import { createWorkspace, Workspace } from "@/lib/api/workspaces";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Building2, ArrowLeft, Loader2 } from "lucide-react";

interface WorkspaceCreateFormProps {
  onSuccess?: (workspace: Workspace) => void;
  onCancel?: () => void;
}

export function WorkspaceCreateForm({
  onSuccess,
  onCancel,
}: WorkspaceCreateFormProps) {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const router = useRouter();
  const { refreshWorkspaces, switchWorkspace } = useWorkspace();
  const { toast } = useToast();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Issue #675 (epic #674) sub-A: there is no pre-submit cap fetch or
  // hard-block UI on this form. The over-cap path surfaces the backend's
  // authoritative error message verbatim in the catch block below — it
  // already includes live ``current owned N (cap: M)`` data which a
  // mount-time cached value cannot reliably match (cap can change between
  // mount and submit via sub-B admin grants, ownership churn, etc.).
  // Sub-A's promise is "backend authoritative, log-only"
  // (``enforce_workspace_cap=False`` until sub-C / #677 flips it).

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const workspace = await createWorkspace({
        name,
        description: description || undefined,
      });

      toast({
        title: t("workspaceCreated"),
        description: t("workspaceCreatedDesc"),
      });

      // Refresh workspaces list
      await refreshWorkspaces();

      // Switch to new workspace
      await switchWorkspace(workspace.id);

      if (onSuccess) {
        onSuccess(workspace);
      } else {
        router.push("/workspace/dashboard");
      }
    } catch (err: unknown) {
      const errorMessage =
        err instanceof Error ? err.message : t("failedToCreateWorkspace");

      if (errorMessage.includes("Workspace limit reached")) {
        // Surface the backend's authoritative message verbatim — it
        // includes live ``owned N (cap: M)`` data that a cached frontend
        // value cannot reliably match. The trade-off is that ja users see
        // the English backend message, but the alternative (cached cap +
        // i18n) showed stale numbers in real scenarios (Copilot review #5).
        setError(errorMessage);
      } else if (
        errorMessage.includes("validation") ||
        errorMessage.includes("Invalid")
      ) {
        setError(t("validationError"));
      } else {
        // Show original error for debugging, but add context
        setError(`${t("failedToCreateWorkspace")}: ${errorMessage}`);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    if (onCancel) {
      onCancel();
    } else {
      router.back();
    }
  };

  return (
    <div className="max-w-2xl mx-auto">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-blue-100 dark:bg-blue-900">
              <Building2 className="h-6 w-6 text-blue-600 dark:text-blue-400" />
            </div>
            <div>
              <CardTitle>{t("createWorkspace")}</CardTitle>
              <CardDescription>{t("settingsDesc")}</CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-6">
            {error && (
              <div className="p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg text-red-700 dark:text-red-400">
                {error}
              </div>
            )}

            {/* Workspace Name */}
            <div className="space-y-2">
              <Label htmlFor="name">{t("workspaceName")} *</Label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t("workspaceNamePlaceholder")}
                required
                disabled={loading}
              />
              <p className="text-sm text-slate-500">{t("workspaceNameHelp")}</p>
            </div>

            {/* Description */}
            <div className="space-y-2">
              <Label htmlFor="description">{t("workspaceDesc")}</Label>
              <Input
                id="description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t("descPlaceholder")}
                disabled={loading}
              />
              <p className="text-sm text-slate-500">{t("descHelp")}</p>
            </div>

            {/* Actions */}
            <div className="flex items-center gap-3 pt-4">
              <Button
                type="submit"
                disabled={loading || !name}
                className="flex items-center gap-2"
              >
                {loading && <Loader2 className="h-4 w-4 animate-spin" />}
                {loading ? t("creating") : t("createWorkspace")}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={handleCancel}
                disabled={loading}
                className="flex items-center gap-2"
              >
                <ArrowLeft className="h-4 w-4" />
                {t("cancel")}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
