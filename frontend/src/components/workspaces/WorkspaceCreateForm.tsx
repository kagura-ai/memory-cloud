"use client";

/**
 * WorkspaceCreateForm Component
 *
 * Issue #276: Form for creating a new workspace.
 * Used in workspace/settings page when ?create=true is set.
 */

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useToast } from "@/hooks/use-toast";
import { apiClient } from "@/lib/api/base";
import { createWorkspace, Workspace } from "@/lib/api/workspaces";
import { CurrentUsage } from "@/lib/api/usage";
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
  const { refreshWorkspaces, switchWorkspace, workspaces } = useWorkspace();
  const { toast } = useToast();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Issue #675 (epic #674): cap is per-user (1 + workspace_slot_bonus),
  // not a tier-derived constant. Fetch it from /api/v1/usage/current on
  // mount. While loading the gate stays permissive — the backend remains
  // the source of truth and rejects over-cap submits via the same
  // "Workspace limit reached" error path.
  const [workspaceLimit, setWorkspaceLimit] = useState<number | null>(null);
  const ownedWorkspaces = workspaces.filter(
    (w) => w.current_user_role === "owner",
  );
  // Guard against ``workspaceLimit <= 0`` (corrupt response): a literal
  // ``limit: 0`` from the API would otherwise make ``length >= 0`` true
  // for every user, including users with zero owned workspaces, walling
  // them off from ever creating one. Treat non-positive limits as
  // "unknown" (permissive) and let the backend gate decide.
  const isLimitReached =
    workspaceLimit !== null &&
    workspaceLimit > 0 &&
    ownedWorkspaces.length >= workspaceLimit;

  useEffect(() => {
    let cancelled = false;
    apiClient
      .get<CurrentUsage>("/api/v1/usage/current")
      .then((usage) => {
        if (!cancelled) setWorkspaceLimit(usage.workspaces.limit);
      })
      .catch(() => {
        // Network/auth failure: leave permissive; backend gate authoritative.
      });
    return () => {
      cancelled = true;
    };
  }, []);

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

      // Parse error and show user-friendly message
      if (
        errorMessage.includes("Workspace limit reached") ||
        errorMessage.includes("limit")
      ) {
        if (workspaceLimit !== null && workspaceLimit > 0) {
          setError(t("workspaceLimitReached", { limit: workspaceLimit }));
        } else {
          // Limit unknown (fetch failed or corrupt response) — show the
          // backend's authoritative message rather than a bogus i18n
          // substitution with a placeholder count.
          setError(errorMessage);
        }
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

  // Issue #276: Show error page if limit reached
  if (isLimitReached) {
    return (
      <div className="max-w-2xl mx-auto">
        <Card>
          <CardHeader>
            <div className="flex items-center gap-3">
              <div className="p-2 rounded-lg bg-red-100 dark:bg-red-900">
                <Building2 className="h-6 w-6 text-red-600 dark:text-red-400" />
              </div>
              <div>
                <CardTitle>
                  {t("workspaceLimitReached", { limit: workspaceLimit ?? 0 })}
                </CardTitle>
                <CardDescription>
                  {t("currentlyOwning", { count: ownedWorkspaces.length })}
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <p className="text-sm text-slate-600 dark:text-slate-400">
                {t("deleteExistingToCreate")}
              </p>
              <Button
                variant="outline"
                onClick={handleCancel}
                className="flex items-center gap-2"
              >
                <ArrowLeft className="h-4 w-4" />
                {tCommon("back")}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

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
