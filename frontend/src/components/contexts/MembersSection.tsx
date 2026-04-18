/**
 * MembersSection
 *
 * Self-contained Settings-tab section for managing ContextMember entries on
 * shared contexts. Issue #362 — restores the regression introduced by PR #159
 * when the edit-modal was merged into the Settings page.
 *
 * Visibility is gated by the caller (`workspace/contexts/[id]/page.tsx`) on
 *   !context.is_private && hasWorkspaceRole(currentWorkspace?.current_user_role, "admin")
 *
 * The component defensively re-checks the same predicate and renders nothing if
 * it is ever rendered outside that envelope.
 */

"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Loader2, Trash2, UserPlus, Users } from "lucide-react";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { TableLoadingState } from "@/components/common/LoadingState";
import { EmptyState } from "@/components/ui/empty-state";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole } from "@/lib/auth/rbac";
import { useToast } from "@/hooks/use-toast";
import { ApiError } from "@/lib/api/base";
import {
  addContextMember,
  listContextMembers,
  removeContextMember,
  updateContextMemberRole,
  type ContextMember,
} from "@/lib/api/contexts";
import { listMembers, type WorkspaceMember } from "@/lib/api/workspaces";
import type { Context } from "@/lib/types/context";

type ContextRole = "owner" | "editor" | "viewer";

interface MembersSectionProps {
  contextId: string;
  context: Context;
}

function isContextRole(role: string): role is ContextRole {
  return role === "owner" || role === "editor" || role === "viewer";
}

export function MembersSection({ contextId, context }: MembersSectionProps) {
  const t = useTranslations("contexts");
  const tCommon = useTranslations("common");
  const { user } = useAuth();
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  const { toast } = useToast();

  const canManage =
    !context.is_private &&
    hasWorkspaceRole(currentWorkspace?.current_user_role, "admin");

  const [members, setMembers] = useState<ContextMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [workspaceMembers, setWorkspaceMembers] = useState<WorkspaceMember[]>(
    [],
  );
  const [workspaceMembersLoaded, setWorkspaceMembersLoaded] = useState(false);
  const [addDialogOpen, setAddDialogOpen] = useState(false);
  const [addSelectedUserId, setAddSelectedUserId] = useState<string>("");
  const [addSelectedRole, setAddSelectedRole] = useState<ContextRole>("editor");
  const [submittingAdd, setSubmittingAdd] = useState(false);

  const [removeTarget, setRemoveTarget] = useState<ContextMember | null>(null);
  const [submittingRemove, setSubmittingRemove] = useState(false);

  const currentUserId = user?.id ?? null;

  const loadMembers = useCallback(async () => {
    try {
      setLoading(true);
      setLoadError(null);
      const result = await listContextMembers(contextId);
      setMembers(result);
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null;
      setLoadError(apiError?.details?.detail || t("loadMembersFailed"));
    } finally {
      setLoading(false);
    }
  }, [contextId, t]);

  useEffect(() => {
    if (!canManage) return;
    loadMembers();
  }, [canManage, loadMembers]);

  // Lazy-load workspace members on first "Add" open. The explicit loaded flag
  // prevents re-fetching on every dialog open when the workspace legitimately
  // has no assignable members (checking .length > 0 would not short-circuit).
  const ensureWorkspaceMembersLoaded = useCallback(async () => {
    if (!currentWorkspaceId || workspaceMembersLoaded) return;
    try {
      const result = await listMembers(currentWorkspaceId);
      setWorkspaceMembers(result);
      setWorkspaceMembersLoaded(true);
    } catch {
      toast({
        title: t("loadWorkspaceMembersFailed"),
        variant: "destructive",
      });
    }
  }, [currentWorkspaceId, workspaceMembersLoaded, toast, t]);

  const assignableWorkspaceMembers = useMemo(() => {
    const existingIds = new Set(members.map((m) => m.user_id));
    return workspaceMembers.filter((wm) => !existingIds.has(wm.user_id));
  }, [members, workspaceMembers]);

  const handleOpenAddDialog = async () => {
    setAddSelectedUserId("");
    setAddSelectedRole("editor");
    setAddDialogOpen(true);
    await ensureWorkspaceMembersLoaded();
  };

  const handleAddSubmit = async () => {
    if (!addSelectedUserId) return;
    try {
      setSubmittingAdd(true);
      await addContextMember(contextId, {
        user_id: addSelectedUserId,
        role: addSelectedRole,
      });
      toast({ title: t("addMemberSuccess") });
      setAddDialogOpen(false);
      await loadMembers();
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null;
      toast({
        title: apiError?.details?.detail || t("addMemberFailed"),
        variant: "destructive",
      });
    } finally {
      setSubmittingAdd(false);
    }
  };

  const handleRoleChange = async (member: ContextMember, newRole: string) => {
    if (!isContextRole(newRole) || newRole === member.role) return;
    const previousRole = member.role;
    // Optimistic update
    setMembers((prev) =>
      prev.map((m) =>
        m.user_id === member.user_id ? { ...m, role: newRole } : m,
      ),
    );
    try {
      await updateContextMemberRole(contextId, member.user_id, {
        role: newRole,
      });
      toast({ title: t("roleUpdateSuccess") });
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null;
      // Rollback
      setMembers((prev) =>
        prev.map((m) =>
          m.user_id === member.user_id ? { ...m, role: previousRole } : m,
        ),
      );
      toast({
        title: apiError?.details?.detail || t("roleUpdateFailed"),
        variant: "destructive",
      });
    }
  };

  const handleRemoveConfirm = async () => {
    if (!removeTarget) return;
    try {
      setSubmittingRemove(true);
      await removeContextMember(contextId, removeTarget.user_id);
      toast({
        title: t("memberRemoved"),
        description: t("memberRemovedSuccess"),
      });
      setRemoveTarget(null);
      await loadMembers();
    } catch (err) {
      const apiError = err instanceof ApiError ? err : null;
      toast({
        title: apiError?.details?.detail || t("removeMemberFailed"),
        variant: "destructive",
      });
    } finally {
      setSubmittingRemove(false);
    }
  };

  if (!canManage) return null;

  // Row-level action gate. Rendering no button is a UX hint; backend still
  // enforces the corresponding 400 guards independently.
  const canModifyMember = (m: ContextMember) =>
    !m.is_workspace_admin && m.role !== "owner" && m.user_id !== currentUserId;

  return (
    <>
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="flex items-center gap-2">
            <Users className="h-5 w-5" />
            {t("contextMembers")}
            <span className="text-sm font-normal text-muted-foreground">
              {t("membersCount", { count: members.length })}
            </span>
          </CardTitle>
          <Button size="sm" onClick={handleOpenAddDialog}>
            <UserPlus className="h-4 w-4 mr-2" />
            {t("addMember")}
          </Button>
        </CardHeader>
        <CardContent>
          {loading ? (
            <TableLoadingState rows={3} />
          ) : loadError ? (
            <ErrorBanner error={loadError} />
          ) : members.length === 0 ? (
            <EmptyState
              compact
              icon={Users}
              title={t("noMembersAssigned")}
              description={t("addFirstMemberHint")}
              actionLabel={t("addMember")}
              onAction={handleOpenAddDialog}
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-xs uppercase text-muted-foreground">
                    <th className="py-2 pr-4 font-medium">{t("memberUser")}</th>
                    <th className="py-2 pr-4 font-medium">
                      {t("memberRoleColumn")}
                    </th>
                    <th className="py-2 w-16"></th>
                  </tr>
                </thead>
                <tbody>
                  {members.map((m) => (
                    <tr
                      key={m.user_id}
                      className="border-b last:border-0"
                      data-testid={`member-row-${m.user_id}`}
                    >
                      <td className="py-3 pr-4">
                        <div className="font-medium">
                          {m.user_email || m.user_name || m.user_id}
                          {m.user_id === currentUserId && (
                            <span className="ml-2 text-xs text-muted-foreground">
                              ({t("youLabel")})
                            </span>
                          )}
                        </div>
                        {m.is_workspace_admin && (
                          <div className="text-xs text-muted-foreground">
                            {t("viaWorkspaceAccess")}
                          </div>
                        )}
                      </td>
                      <td className="py-3 pr-4">
                        {canModifyMember(m) ? (
                          <select
                            className="rounded border bg-background px-2 py-1 text-sm"
                            value={m.role}
                            onChange={(e) =>
                              handleRoleChange(m, e.target.value)
                            }
                            aria-label={t("memberRoleColumn")}
                          >
                            <option value="editor">
                              {t("contextRoleEditor")}
                            </option>
                            <option value="viewer">{t("viewer")}</option>
                          </select>
                        ) : (
                          <span className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                            {t(roleBadgeKey(m))}
                          </span>
                        )}
                      </td>
                      <td className="py-3 text-right">
                        {canModifyMember(m) && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setRemoveTarget(m)}
                            aria-label={t("removeMember", {
                              name: m.user_email || m.user_id,
                            })}
                          >
                            <Trash2 className="h-4 w-4 text-red-600" />
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-3 text-xs text-muted-foreground">
                {t("workspaceAccessNote")}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Add member dialog */}
      <Dialog open={addDialogOpen} onOpenChange={setAddDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("addMemberTitle")}</DialogTitle>
            <DialogDescription>{t("addMemberDescription")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t("selectWorkspaceMember")}
              </label>
              {assignableWorkspaceMembers.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  {t("noWorkspaceMembersToAdd")}
                </p>
              ) : (
                <select
                  className="w-full rounded border bg-background px-2 py-2 text-sm"
                  value={addSelectedUserId}
                  onChange={(e) => setAddSelectedUserId(e.target.value)}
                  aria-label={t("selectWorkspaceMember")}
                >
                  <option value="" disabled>
                    {t("selectWorkspaceMemberPlaceholder")}
                  </option>
                  {assignableWorkspaceMembers.map((wm) => (
                    <option key={wm.user_id} value={wm.user_id}>
                      {wm.user_email || wm.user_name || wm.user_id}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t("contextRoleLabel")}
              </label>
              <select
                className="w-full rounded border bg-background px-2 py-2 text-sm"
                value={addSelectedRole}
                onChange={(e) => {
                  const v = e.target.value;
                  if (isContextRole(v)) setAddSelectedRole(v);
                }}
                aria-label={t("contextRoleLabel")}
              >
                <option value="editor">{t("contextRoleEditor")}</option>
                <option value="viewer">{t("viewer")}</option>
              </select>
            </div>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setAddDialogOpen(false)}
              disabled={submittingAdd}
            >
              {tCommon("cancel")}
            </Button>
            <Button
              onClick={handleAddSubmit}
              disabled={submittingAdd || !addSelectedUserId}
            >
              {submittingAdd && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {t("addMember")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Remove confirmation */}
      <AlertDialog
        open={removeTarget !== null}
        onOpenChange={(open) => !open && setRemoveTarget(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("removeMemberTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {removeTarget
                ? t("removeMemberDesc", {
                    name:
                      removeTarget.user_email ||
                      removeTarget.user_name ||
                      removeTarget.user_id,
                  })
                : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={submittingRemove}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleRemoveConfirm}
              className="bg-red-600 hover:bg-red-700"
              disabled={submittingRemove}
            >
              {submittingRemove && (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              )}
              {tCommon("remove")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function roleBadgeKey(m: ContextMember): string {
  // Show the actual role on every row. "via workspace access" rendered as
  // the secondary hint already disambiguates workspace-derived rows.
  switch (m.role) {
    case "owner":
      return "owner";
    case "admin":
      return "admin";
    case "member":
      return "member";
    case "editor":
      return "contextRoleEditor";
    case "viewer":
      return "viewer";
    default:
      return "contextMember";
  }
}
