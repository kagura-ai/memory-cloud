"use client";

/**
 * Workspace Members Management Page
 *
 * Issue #115 Phase B-5: Workspace-level Multi-tenancy Frontend
 *
 * Manage workspace members, roles, and invitations.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import { copyText } from "@/lib/utils/clipboard";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import { ActionButton } from "@/components/common/ActionButton";
import { Button } from "@/components/ui/button";
import {
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useAuth } from "@/contexts/AuthContext";
import {
  listMembers,
  addMember,
  updateMemberRole,
  removeMember,
  updateMemberContextAccess,
  WorkspaceMember,
} from "@/lib/api/workspaces";
import { getContexts, Context } from "@/lib/api/contexts";
import { ApiError } from "@/lib/api/base";
import {
  listInvitations,
  createInvitation,
  deleteInvitation,
  WorkspaceInvitation,
  getMemberQuota,
  MemberQuota,
} from "@/lib/api/invitations";
import {
  UserPlus,
  Trash2,
  Shield,
  Users,
  Copy,
  Check,
  X,
  AlertTriangle,
  Settings,
  ChevronDown,
  ChevronUp,
} from "lucide-react";
import Link from "next/link";
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
import { useToast } from "@/hooks/use-toast";

export default function WorkspaceMembersPage() {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const router = useRouter();
  const {
    currentWorkspaceId,
    currentWorkspace,
    loading: workspaceLoading,
  } = useWorkspace();
  const { user } = useAuth();
  const { toast } = useToast();

  // Issue #398: admin/owner only — redirect member/viewer to dashboard.
  // Mirrors settings/general/page.tsx:83-90 pattern. The workspaceLoading
  // guard prevents a flash-redirect while WorkspaceContext hydrates.
  useEffect(() => {
    if (workspaceLoading) return;
    if (
      currentWorkspace &&
      !hasWorkspaceRole(currentWorkspace.current_user_role, WorkspaceRole.Admin)
    ) {
      router.push("/workspace/dashboard");
    }
  }, [currentWorkspace, workspaceLoading, router]);
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [invitations, setInvitations] = useState<WorkspaceInvitation[]>([]);
  const [loading, setLoading] = useState(true);
  const [showInviteDialog, setShowInviteDialog] = useState(false);
  const [showUpgradePrompt, setShowUpgradePrompt] = useState(false); // Issue #165: Pro plan gate

  // Invite dialog state
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "admin" | "viewer">(
    "member",
  );
  const [inviteExpiry, setInviteExpiry] = useState<number | null>(30);
  const [inviteContextIds, setInviteContextIds] = useState<string[]>([]); // Migration 042
  const [createdInviteUrl, setCreatedInviteUrl] = useState<string | null>(null);
  const [copiedToken, setCopiedToken] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string | null>(null); // Issue #217
  const [inviteLoading, setInviteLoading] = useState(false); // Issue #217

  // Issue #217: Delete member confirmation dialog
  const [memberToDelete, setMemberToDelete] = useState<WorkspaceMember | null>(
    null,
  );
  const [showDeleteMemberDialog, setShowDeleteMemberDialog] = useState(false);
  const [deleteLoading, setDeleteLoading] = useState(false);

  // Issue #217: Delete invitation confirmation dialog
  const [invitationToDelete, setInvitationToDelete] =
    useState<WorkspaceInvitation | null>(null);
  const [showDeleteInvitationDialog, setShowDeleteInvitationDialog] =
    useState(false);
  const [deleteInvitationLoading, setDeleteInvitationLoading] = useState(false);

  // Issue #229: Member quota state
  const [memberQuota, setMemberQuota] = useState<MemberQuota | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);

  // Issue #234: Context access restriction
  const [contexts, setContexts] = useState<Context[]>([]);
  const [contextAccessMember, setContextAccessMember] =
    useState<WorkspaceMember | null>(null);
  const [showContextAccessDialog, setShowContextAccessDialog] = useState(false);
  const [selectedContextIds, setSelectedContextIds] = useState<string[]>([]);
  const [contextAccessLoading, setContextAccessLoading] = useState(false);

  // Issue #234: Role change confirmation (clears context access)
  const [pendingRoleChange, setPendingRoleChange] = useState<{
    member: WorkspaceMember;
    newRole: string;
  } | null>(null);
  const [showRoleChangeDialog, setShowRoleChangeDialog] = useState(false);
  const [roleChangeLoading, setRoleChangeLoading] = useState(false);

  // Role permissions section toggle
  const [showRolePermissions, setShowRolePermissions] = useState(false);

  // Check if Pro plan
  const isProPlan = currentWorkspace?.plan_name === "pro";

  useEffect(() => {
    // Issue #398: skip the four protected fetches for member/viewer — the
    // redirect useEffect above is sending them to /dashboard, and the backend
    // would 403 each call. Without this guard a non-admin briefly hits four
    // protected endpoints in parallel with the redirect, surfacing spurious
    // error toasts before the route change lands.
    if (workspaceLoading) return;
    if (
      !hasWorkspaceRole(
        currentWorkspace?.current_user_role,
        WorkspaceRole.Admin,
      )
    )
      return;
    if (currentWorkspaceId) {
      loadMembers();
      loadInvitations();
      loadMemberQuota(); // Issue #229
      loadContexts(); // Issue #234
    }
    // Depend on the role string (a primitive) rather than the whole
    // currentWorkspace object — a context provider re-render with the same
    // workspace would otherwise re-trigger four protected fetches.
  }, [
    currentWorkspaceId,
    currentWorkspace?.current_user_role,
    workspaceLoading,
  ]);

  const loadMembers = async () => {
    if (!currentWorkspaceId) return;

    try {
      setLoading(true);
      const data = await listMembers(currentWorkspaceId);

      // Migration 034: Sort members - current user first
      const sortedMembers = data.sort((a, b) => {
        if (a.user_id === user?.id) return -1;
        if (b.user_id === user?.id) return 1;
        return 0;
      });

      setMembers(sortedMembers);
    } catch (error) {
      console.error("Failed to load members:", error);
    } finally {
      setLoading(false);
    }
  };

  const loadInvitations = async () => {
    if (!currentWorkspaceId) return;

    try {
      const data = await listInvitations(currentWorkspaceId, false); // Only pending invitations
      setInvitations(data);
    } catch (error: unknown) {
      // 403 = Not admin/owner, silently skip invitations feature
      if (error instanceof ApiError && error.status === 403) {
        setInvitations([]);
        return;
      }
      console.error("Failed to load invitations:", error);
    }
  };

  // Issue #229: Load member quota
  const loadMemberQuota = async () => {
    if (!currentWorkspaceId) return;

    try {
      setQuotaLoading(true);
      const quota = await getMemberQuota(currentWorkspaceId);
      setMemberQuota(quota);
    } catch (error: unknown) {
      // Silently fail if user doesn't have access
      if (error instanceof ApiError && error.status === 403) {
        setMemberQuota(null);
        return;
      }
      console.error("Failed to load member quota:", error);
    } finally {
      setQuotaLoading(false);
    }
  };

  // Issue #234: Load contexts for context access dialog (shared only)
  const loadContexts = async () => {
    try {
      const response = await getContexts();
      // Only show shared contexts - private contexts are owner-only anyway
      const sharedContexts = response.contexts.filter((c) => !c.is_private);
      setContexts(sharedContexts);
    } catch (error) {
      console.error("Failed to load contexts:", error);
    }
  };

  // Issue #234: Handle context access dialog open
  const handleContextAccessClick = (member: WorkspaceMember) => {
    setContextAccessMember(member);
    // Set selected contexts (empty array if NULL/unrestricted)
    setSelectedContextIds(member.allowed_context_ids || []);
    setShowContextAccessDialog(true);
  };

  // Issue #234: Handle context access save
  const handleSaveContextAccess = async () => {
    if (!currentWorkspaceId || !contextAccessMember) return;

    // Migration 042: Require at least 1 context (same as invitation)
    if (selectedContextIds.length === 0) {
      toast({
        title: tCommon("error"),
        description: t("inviteContextRequired"),
        variant: "destructive",
      });
      return;
    }

    setContextAccessLoading(true);
    try {
      // Always use explicit array (NULL not allowed)
      await updateMemberContextAccess(
        currentWorkspaceId,
        contextAccessMember.user_id,
        selectedContextIds,
      );

      // Reload members to get updated data
      await loadMembers();

      toast({
        title: t("contextAccessUpdated"),
        description: t("contextsSelected", {
          count: selectedContextIds.length,
        }),
      });

      setShowContextAccessDialog(false);
    } catch (error: unknown) {
      toast({
        title: t("contextAccessUpdateFailed"),
        description: error instanceof Error ? error.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setContextAccessLoading(false);
    }
  };

  // Issue #234: Toggle context selection
  const toggleContextSelection = (contextId: string) => {
    setSelectedContextIds((prev) =>
      prev.includes(contextId)
        ? prev.filter((id) => id !== contextId)
        : [...prev, contextId],
    );
  };

  const handleCreateInvitation = async () => {
    if (!currentWorkspaceId) return;

    // Validate email is required
    if (!inviteEmail.trim()) {
      setInviteError(t("emailRequiredError"));
      return;
    }

    // Migration 042: Validate context selection for member/viewer
    if (
      (inviteRole === "member" || inviteRole === "viewer") &&
      inviteContextIds.length === 0
    ) {
      setInviteError(t("inviteContextRequired"));
      return;
    }

    setInviteLoading(true);
    setInviteError(null);

    try {
      const invitation = await createInvitation(currentWorkspaceId, {
        email: inviteEmail,
        role: inviteRole,
        expires_in_days: inviteExpiry,
        allowed_context_ids:
          inviteRole === "member" || inviteRole === "viewer"
            ? inviteContextIds
            : null, // Migration 042
      });

      setCreatedInviteUrl(invitation.invitation_url);
      await loadInvitations();
      await loadMemberQuota(); // Issue #229: Reload quota after invitation
      toast({
        title: t("invitationCreated"),
        description: t("invitationSentTo", { email: inviteEmail }),
      });
    } catch (error: unknown) {
      console.error("Failed to create invitation:", error);
      const apiErr = error instanceof ApiError ? error : null;
      const errorMsg =
        apiErr?.details?.detail ||
        (error instanceof Error
          ? error.message
          : "Failed to create invitation");
      setInviteError(errorMsg);
    } finally {
      setInviteLoading(false);
    }
  };

  const handleCopyInviteUrl = async (url: string, token: string) => {
    try {
      // copyText degrades to an execCommand fallback before throwing (#987).
      await copyText(url);
      setCopiedToken(token);
      setTimeout(() => setCopiedToken(null), 2000);
    } catch (error) {
      console.error("Failed to copy URL:", error);
      toast({
        title: tCommon("error"),
        description: tCommon("copyFailedManualHint"),
        variant: "destructive",
      });
    }
  };

  const handleCloseInviteDialog = () => {
    setShowInviteDialog(false);
    setCreatedInviteUrl(null);
    setInviteEmail("");
    setInviteRole("member");
    setInviteExpiry(30);
    setInviteError(null); // Issue #217: Clear error on close
  };

  // Issue #217: Open delete invitation confirmation dialog
  const handleDeleteInvitationClick = (invitation: WorkspaceInvitation) => {
    setInvitationToDelete(invitation);
    setShowDeleteInvitationDialog(true);
  };

  // Issue #217: Confirm and delete invitation
  const handleConfirmDeleteInvitation = async () => {
    if (!currentWorkspaceId || !invitationToDelete) return;

    setDeleteInvitationLoading(true);
    try {
      await deleteInvitation(currentWorkspaceId, invitationToDelete.id);
      await loadInvitations();
      await loadMemberQuota(); // Issue #229: Reload quota after deletion
      setShowDeleteInvitationDialog(false);
      setInvitationToDelete(null);
      toast({
        title: t("invitationRevoked"),
        description: t("invitationRevokedDesc", {
          email: invitationToDelete.email || "user",
        }),
      });
    } catch (error: unknown) {
      console.error("Failed to delete invitation:", error);
      const apiErr = error instanceof ApiError ? error : null;
      const errorMsg =
        apiErr?.details?.detail ||
        (error instanceof Error
          ? error.message
          : t("failedToRevokeInvitation"));
      toast({
        title: t("failedToRevokeInvitation"),
        description: errorMsg,
        variant: "destructive",
      });
    } finally {
      setDeleteInvitationLoading(false);
    }
  };

  // Issue #217: Open delete member confirmation dialog
  const handleRemoveMemberClick = (member: WorkspaceMember) => {
    setMemberToDelete(member);
    setShowDeleteMemberDialog(true);
  };

  // Issue #217: Confirm and remove member
  const handleConfirmRemoveMember = async () => {
    if (!currentWorkspaceId || !memberToDelete) return;

    setDeleteLoading(true);
    try {
      await removeMember(currentWorkspaceId, memberToDelete.user_id);
      await loadMembers();
      setShowDeleteMemberDialog(false);
      setMemberToDelete(null);
      toast({
        title: t("memberRemoved"),
        description: t("memberRemovedDesc", {
          name:
            memberToDelete.user_name || memberToDelete.user_email || "Member",
        }),
      });
    } catch (error: unknown) {
      console.error("Failed to remove member:", error);
      const apiErr = error instanceof ApiError ? error : null;
      const errorMsg =
        apiErr?.details?.detail ||
        (error instanceof Error ? error.message : t("failedToRemoveMember"));
      toast({
        title: t("failedToRemoveMember"),
        description: errorMsg,
        variant: "destructive",
      });
    } finally {
      setDeleteLoading(false);
    }
  };

  const handleUpdateRole = async (userId: string, newRole: string) => {
    if (!currentWorkspaceId) return;

    // Issue #234: Check if member has context restrictions that will be cleared
    const member = members.find((m) => m.user_id === userId);
    if (
      member &&
      member.allowed_context_ids !== null &&
      member.allowed_context_ids !== undefined
    ) {
      // Show confirmation dialog
      setPendingRoleChange({ member, newRole });
      setShowRoleChangeDialog(true);
      return;
    }

    // No restrictions, proceed directly
    await executeRoleChange(userId, newRole);
  };

  // Issue #234: Execute role change after confirmation
  const executeRoleChange = async (userId: string, newRole: string) => {
    if (!currentWorkspaceId) return;

    setRoleChangeLoading(true);
    try {
      await updateMemberRole(currentWorkspaceId, userId, {
        role: newRole as WorkspaceRole,
      });
      await loadMembers();
      setShowRoleChangeDialog(false);
      setPendingRoleChange(null);
    } catch (error: unknown) {
      console.error("Failed to update role:", error);

      let errorMessage =
        error instanceof Error ? error.message : "Failed to update role";

      if (error instanceof ApiError && error.status === 403) {
        if (error.message.includes("own role")) {
          errorMessage = t("cannotModifyOwnRoleDesc");
        } else if (error.message.includes("owner can change")) {
          errorMessage = t("onlyOwnerCanChangeOwner");
        }
      }

      toast({
        title: tCommon("error"),
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setRoleChangeLoading(false);
    }
  };

  const handleInviteClick = () => {
    // Issue #165: Check Pro plan before showing invite dialog
    if (!isProPlan) {
      setShowUpgradePrompt(true);
      return;
    }
    // Migration 042: Initialize with all shared contexts selected
    const sharedContextIds = contexts
      .filter((c) => !c.is_private)
      .map((c) => c.id);
    setInviteContextIds(sharedContextIds);
    setShowInviteDialog(true);
  };

  if (workspaceLoading || loading) {
    return (
      <PageContainer>
        <PageHeader title={t("membersTitle")} />
        <TableLoadingState rows={5} />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader title={t("membersTitle")} description={t("membersDesc")} />

      <Section
        title={t("membersSection")}
        description={
          members.length !== 1
            ? t("membersCount", { count: members.length })
            : t("memberCount", { count: members.length })
        }
        headerActions={
          <ActionButton
            onClick={handleInviteClick}
            icon={<UserPlus className="w-4 h-4" />}
            disabled={
              !isProPlan ||
              currentWorkspace?.current_user_role === "member" ||
              currentWorkspace?.current_user_role === "viewer"
            }
          >
            {t("inviteMember")}{" "}
            {!isProPlan
              ? t("proPlanRequired")
              : currentWorkspace?.current_user_role === "member" ||
                  currentWorkspace?.current_user_role === "viewer"
                ? t("ownerAdminOnly")
                : ""}
          </ActionButton>
        }
      >
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead>
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                  {t("user")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                  {t("role")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                  {t("joinedAt")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                  {t("lastLogin")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                  {t("contextAccess")}
                </th>
                <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                  {t("authentication")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {members.map((member, index) => (
                <tr
                  key={member.user_id}
                  className={`hover:bg-gray-100 dark:hover:bg-gray-700 ${
                    index % 2 === 0
                      ? "bg-white dark:bg-gray-900"
                      : "bg-gray-50 dark:bg-gray-800/50"
                  }`}
                >
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-full bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white text-sm font-bold">
                        {(
                          member.user_name ||
                          member.user_email ||
                          member.user_id
                        )
                          .charAt(0)
                          .toUpperCase()}
                      </div>
                      <div>
                        <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                          {member.user_name ||
                            member.user_email ||
                            member.user_id}
                        </div>
                        {member.user_email && !member.user_name && (
                          <div className="text-xs text-gray-500 dark:text-gray-400">
                            {member.user_email}
                          </div>
                        )}
                        {member.user_name && member.user_email && (
                          <div className="text-xs text-gray-500 dark:text-gray-400">
                            {member.user_email}
                          </div>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    {member.role === "owner" ? (
                      <span className="px-3 py-1 text-sm font-medium bg-purple-100 dark:bg-purple-900 text-purple-800 dark:text-purple-200 rounded">
                        {t("ownerCannotChange")}
                      </span>
                    ) : (
                      <select
                        value={member.role}
                        onChange={(e) =>
                          handleUpdateRole(member.user_id, e.target.value)
                        }
                        disabled={
                          currentWorkspace?.current_user_role === "member" ||
                          currentWorkspace?.current_user_role === "viewer" ||
                          member.user_id === user?.id
                        }
                        className="text-sm border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-800 disabled:opacity-50 disabled:cursor-not-allowed"
                        title={
                          member.user_id === user?.id
                            ? t("cannotModifyOwnRole")
                            : undefined
                        }
                      >
                        <option value="admin">{t("admin")}</option>
                        <option value="member">{t("member")}</option>
                        <option value="viewer">{t("viewer")}</option>
                      </select>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {member.joined_at
                      ? new Date(member.joined_at).toLocaleDateString()
                      : "N/A"}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                    {member.last_login_at ? (
                      <span>
                        {new Date(member.last_login_at).toLocaleString()}
                      </span>
                    ) : (
                      <span className="text-gray-400">{t("never")}</span>
                    )}
                  </td>
                  {/* Issue #234: Context Access column */}
                  <td className="px-6 py-4 whitespace-nowrap text-sm">
                    {member.role === "owner" || member.role === "admin" ? (
                      <span className="text-gray-400 text-xs">
                        {t("contextAccessNotApplicable")}
                      </span>
                    ) : member.allowed_context_ids === null ||
                      member.allowed_context_ids === undefined ? (
                      <button
                        onClick={() => handleContextAccessClick(member)}
                        disabled={
                          currentWorkspace?.current_user_role !== "owner" &&
                          currentWorkspace?.current_user_role !== "admin"
                        }
                        className="flex items-center gap-1 text-amber-600 hover:text-amber-700 dark:text-amber-400 dark:hover:text-amber-300 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        <AlertTriangle className="w-4 h-4" />
                        <span className="text-xs font-medium">
                          {t("contextNotConfigured")}
                        </span>
                      </button>
                    ) : (
                      <button
                        onClick={() => handleContextAccessClick(member)}
                        disabled={
                          currentWorkspace?.current_user_role !== "owner" &&
                          currentWorkspace?.current_user_role !== "admin"
                        }
                        className="flex items-center gap-1 text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        <Settings className="w-4 h-4" />
                        <span className="text-xs">
                          {t("contextsSelected", {
                            count: member.allowed_context_ids.length,
                          })}
                        </span>
                      </button>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-sm">
                    {/* Issue #350: Remove button only renders for non-owner
                        rows when the viewer is an owner. Every other case
                        (owner rows, admin-viewing-admin rows, self rows)
                        falls through to a centered muted em-dash so the
                        actions column never reads as "still loading" to
                        users. Remove button stays right-aligned; em-dash
                        centers in the cell. */}
                    {member.role !== "owner" &&
                    currentWorkspace?.current_user_role === "owner" ? (
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => handleRemoveMemberClick(member)}
                          className="text-red-600 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300"
                          title={t("removeTitle")}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    ) : (
                      <span className="block text-center text-gray-400 text-xs">
                        —
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {members.length === 0 && (
          <div className="text-center py-12 text-gray-500 dark:text-gray-400">
            <Users className="w-12 h-12 mx-auto mb-4 opacity-50" />
            <p>{t("noMembersYet")}</p>
          </div>
        )}
      </Section>

      {/* Pending Invitations Section */}
      {invitations.length > 0 && (
        <Section
          title={t("pendingInvitations")}
          description={
            invitations.length !== 1
              ? t("invitationsCount", { count: invitations.length })
              : t("invitationCount", { count: invitations.length })
          }
        >
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead>
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    {t("emailAnyone")}
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    {t("role")}
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    {t("contextAccess")}
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    {t("expires")}
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    {t("invitationLink")}
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">
                    {tCommon("actions")}
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                {invitations.map((invitation) => (
                  <tr
                    key={invitation.id}
                    className="hover:bg-gray-50 dark:hover:bg-gray-800"
                  >
                    <td className="px-6 py-4 whitespace-nowrap text-sm">
                      <span className="text-gray-900 dark:text-gray-100">
                        {invitation.email || "N/A"}
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span className="px-2 py-1 text-xs font-medium bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-200 rounded-full">
                        {invitation.role}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-sm">
                      {invitation.role === "admin" ? (
                        <span className="text-gray-400 text-xs">
                          {t("contextAccessNotApplicable")}
                        </span>
                      ) : invitation.allowed_context_ids &&
                        invitation.allowed_context_ids.length > 0 ? (
                        <div className="flex flex-wrap gap-1">
                          {invitation.allowed_context_ids
                            .slice(0, 3)
                            .map((ctxId) => {
                              const ctx = contexts.find((c) => c.id === ctxId);
                              return (
                                <span
                                  key={ctxId}
                                  className="px-2 py-0.5 text-xs bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-200 rounded"
                                >
                                  {ctx?.display_name ||
                                    ctx?.name ||
                                    ctxId.substring(0, 8)}
                                </span>
                              );
                            })}
                          {invitation.allowed_context_ids.length > 3 && (
                            <span className="text-xs text-gray-500 dark:text-gray-400">
                              +{invitation.allowed_context_ids.length - 3}
                            </span>
                          )}
                        </div>
                      ) : (
                        <span className="text-amber-600 dark:text-amber-400 text-xs flex items-center gap-1">
                          <AlertTriangle className="w-3 h-3" />
                          {t("contextNotConfigured")}
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm">
                      {invitation.expires_at ? (
                        <span
                          className={
                            invitation.is_expired
                              ? "text-red-600 dark:text-red-400"
                              : "text-gray-700 dark:text-gray-300"
                          }
                        >
                          {new Date(invitation.expires_at).toLocaleDateString()}
                        </span>
                      ) : (
                        <span className="text-gray-500 dark:text-gray-400">
                          {t("neverExpires")}
                        </span>
                      )}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm">
                      <button
                        onClick={() =>
                          handleCopyInviteUrl(
                            invitation.invitation_url,
                            invitation.token,
                          )
                        }
                        className="flex items-center gap-2 text-blue-600 dark:text-blue-400 hover:text-blue-700 dark:hover:text-blue-300"
                      >
                        {copiedToken === invitation.token ? (
                          <>
                            <Check className="w-4 h-4 text-green-600 dark:text-green-400" />
                            <span className="text-green-600 dark:text-green-400">
                              {t("copied")}
                            </span>
                          </>
                        ) : (
                          <>
                            <Copy className="w-4 h-4" />
                            <span>{t("copyLink")}</span>
                          </>
                        )}
                      </button>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm">
                      <button
                        onClick={() => handleDeleteInvitationClick(invitation)}
                        className="text-red-600 hover:text-red-700 dark:text-red-400 dark:hover:text-red-300"
                        title={t("revokeInvitation")}
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      {/* Role Permissions Section */}
      <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
        <button
          onClick={() => setShowRolePermissions(!showRolePermissions)}
          className="w-full px-6 py-4 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
        >
          <div className="flex items-center gap-3">
            <div className="p-2 bg-purple-100 dark:bg-purple-900/30 rounded-lg">
              <Shield className="h-5 w-5 text-purple-600 dark:text-purple-400" />
            </div>
            <div className="text-left">
              <h3 className="text-base font-semibold text-gray-900 dark:text-gray-100">
                {t("rolePermissions")}
              </h3>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {t("rolePermissionsDesc")}
              </p>
            </div>
          </div>
          {showRolePermissions ? (
            <ChevronUp className="h-5 w-5 text-gray-400" />
          ) : (
            <ChevronDown className="h-5 w-5 text-gray-400" />
          )}
        </button>

        {showRolePermissions && (
          <div className="px-6 pb-6 border-t border-gray-200 dark:border-gray-700 pt-4">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead>
                  <tr className="bg-gray-50 dark:bg-gray-800/50">
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      {t("permission")}
                    </th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      {t("ownerRole")}
                    </th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      {t("adminRole")}
                    </th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      {t("memberRole")}
                    </th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      {t("viewerRole")}
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                  <PermissionRow
                    permission={t("permissionManageWorkspace")}
                    owner={true}
                    admin={false}
                    member={false}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionInviteMembers")}
                    owner={true}
                    admin={true}
                    member={false}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionManageRoles")}
                    owner={true}
                    admin={true}
                    member={false}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionManageContexts")}
                    owner={true}
                    admin={true}
                    member={false}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionManagePublicSettings")}
                    owner={true}
                    admin={false}
                    member={false}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionAccessAllContexts")}
                    owner={true}
                    admin={true}
                    member={false}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionAccessAssignedContexts")}
                    owner={false}
                    admin={false}
                    member={true}
                    viewer={true}
                  />
                  <PermissionRow
                    permission={t("permissionCreateMemories")}
                    owner={true}
                    admin={true}
                    member={true}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionDeleteOwnMemories")}
                    owner={true}
                    admin={true}
                    member={true}
                    viewer={false}
                  />
                  <PermissionRow
                    permission={t("permissionReadMemories")}
                    owner={true}
                    admin={true}
                    member={true}
                    viewer={true}
                  />
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* Invite Dialog */}
      {showInviteDialog && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-md w-full mx-4">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                {t("inviteTeamMember")}
              </h3>
              <button
                onClick={handleCloseInviteDialog}
                className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {!createdInviteUrl ? (
              // Step 1: Create invitation form
              <div className="space-y-4">
                {/* Issue #229: Seat usage badge */}
                {memberQuota && (
                  <div
                    className={`p-3 rounded-lg border ${
                      memberQuota.percentage >= 100
                        ? "bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800"
                        : memberQuota.percentage >= 80
                          ? "bg-yellow-50 dark:bg-yellow-900/20 border-yellow-200 dark:border-yellow-800"
                          : "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium">
                          {memberQuota.percentage >= 100
                            ? "❌"
                            : memberQuota.percentage >= 80
                              ? "⚠️"
                              : "ℹ️"}{" "}
                          {t("seatUsage", {
                            used: memberQuota.total_used,
                            limit: memberQuota.limit,
                          })}
                        </p>
                        <p className="text-xs mt-1">
                          {memberQuota.available > 0
                            ? t("seatsAvailable", {
                                available: memberQuota.available,
                              })
                            : t("seatLimitReached")}
                        </p>
                      </div>
                      {memberQuota.percentage >= 100 && (
                        <Link href="/workspace/plan">
                          <button className="px-3 py-1 text-xs bg-red-600 text-white rounded hover:bg-red-700">
                            {t("upgradeToAddMembers")}
                          </button>
                        </Link>
                      )}
                    </div>
                  </div>
                )}

                {memberQuota && memberQuota.percentage >= 100 ? (
                  // At limit - show upgrade prompt instead of form
                  <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4 text-center">
                    <AlertTriangle className="w-12 h-12 text-red-600 mx-auto mb-3" />
                    <h4 className="text-lg font-semibold text-red-900 dark:text-red-100 mb-2">
                      {t("seatLimitReached")}
                    </h4>
                    <p className="text-sm text-red-800 dark:text-red-200 mb-4">
                      {t("seatLimitReachedDesc", {
                        plan:
                          currentWorkspace?.plan_name?.toUpperCase() || "PRO",
                        limit: memberQuota.limit,
                      })}
                    </p>
                    <Link href="/workspace/plan">
                      <button className="px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700">
                        {t("upgradeToAddMembers")}
                      </button>
                    </Link>
                  </div>
                ) : (
                  <>
                    <div>
                      <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">
                        {t("emailRequired")}{" "}
                        <span className="text-red-500">*</span>
                      </label>
                      <input
                        type="email"
                        value={inviteEmail}
                        onChange={(e) => setInviteEmail(e.target.value)}
                        placeholder={t("emailPlaceholder")}
                        required
                        className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      />
                      <div className="mt-1 p-2 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded">
                        <p className="text-xs text-blue-900 dark:text-blue-100 font-medium">
                          ℹ️ {t("emailImportant")}
                        </p>
                        <p className="text-xs text-blue-800 dark:text-blue-200 mt-1">
                          {t("emailMustMatch")}
                        </p>
                      </div>
                    </div>

                    <div>
                      <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">
                        {t("role")}
                      </label>
                      <select
                        value={inviteRole}
                        onChange={(e) => setInviteRole(e.target.value as any)}
                        className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      >
                        <option value="member">{t("roleMemberDesc")}</option>
                        <option value="admin">{t("roleAdminDesc")}</option>
                        <option value="viewer">{t("roleViewerDesc")}</option>
                      </select>
                    </div>

                    <div>
                      <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">
                        {t("expiresIn")}
                      </label>
                      <select
                        value={inviteExpiry ?? "never"}
                        onChange={(e) =>
                          setInviteExpiry(
                            e.target.value === "never"
                              ? null
                              : Number(e.target.value),
                          )
                        }
                        className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100"
                      >
                        <option value="7">{t("days7")}</option>
                        <option value="30">{t("days30")}</option>
                        <option value="90">{t("days90")}</option>
                        <option value="365">{t("year1")}</option>
                        <option value="never">{t("neverExpires")}</option>
                      </select>
                    </div>

                    {/* Migration 042: Context Access Selection (for member/viewer only) */}
                    {(inviteRole === "member" || inviteRole === "viewer") && (
                      <div>
                        <label className="block text-sm font-medium mb-2 text-gray-700 dark:text-gray-300">
                          {t("inviteContextSelection")}
                        </label>
                        <p className="text-xs text-gray-600 dark:text-gray-400 mb-2">
                          {t("inviteContextSelectionDesc")}
                        </p>

                        {/* Shared contexts check */}
                        {contexts.filter((c) => !c.is_private).length === 0 ? (
                          <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg p-3">
                            <p className="text-sm text-amber-800 dark:text-amber-200">
                              ⚠️ {t("noSharedContexts")}
                            </p>
                            <p className="text-xs text-amber-700 dark:text-amber-300 mt-1">
                              {t("noSharedContextsDesc")}
                            </p>
                          </div>
                        ) : (
                          <>
                            {/* Select All / Deselect All buttons */}
                            <div className="flex items-center gap-2 mb-2">
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() => {
                                  const sharedIds = contexts
                                    .filter((c) => !c.is_private)
                                    .map((c) => c.id);
                                  setInviteContextIds(sharedIds);
                                }}
                              >
                                {t("selectAll")}
                              </Button>
                              <Button
                                type="button"
                                variant="outline"
                                size="sm"
                                onClick={() => setInviteContextIds([])}
                                disabled={inviteContextIds.length === 0}
                              >
                                {t("deselectAll")}
                              </Button>
                            </div>

                            {/* Context checkboxes */}
                            <div className="border border-gray-200 dark:border-gray-700 rounded-lg max-h-40 overflow-y-auto">
                              <ul className="divide-y divide-gray-200 dark:divide-gray-700">
                                {contexts
                                  .filter((c) => !c.is_private)
                                  .map((context) => (
                                    <li
                                      key={context.id}
                                      className="px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-800"
                                    >
                                      <label className="flex items-center gap-2 cursor-pointer">
                                        <input
                                          type="checkbox"
                                          checked={inviteContextIds.includes(
                                            context.id,
                                          )}
                                          onChange={() => {
                                            if (
                                              inviteContextIds.includes(
                                                context.id,
                                              )
                                            ) {
                                              setInviteContextIds(
                                                inviteContextIds.filter(
                                                  (id) => id !== context.id,
                                                ),
                                              );
                                            } else {
                                              setInviteContextIds([
                                                ...inviteContextIds,
                                                context.id,
                                              ]);
                                            }
                                          }}
                                          className="w-4 h-4 text-blue-600 rounded"
                                        />
                                        <span className="text-sm text-gray-900 dark:text-gray-100">
                                          {context.display_name || context.name}
                                        </span>
                                      </label>
                                    </li>
                                  ))}
                              </ul>
                            </div>

                            {/* Status message */}
                            <p className="text-xs text-gray-600 dark:text-gray-400 mt-2">
                              {t("contextsSelected", {
                                count: inviteContextIds.length,
                              })}
                            </p>
                          </>
                        )}
                      </div>
                    )}

                    {/* Issue #217: Error display */}
                    {inviteError && (
                      <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-3">
                        <div className="flex items-start gap-2">
                          <AlertTriangle className="w-4 h-4 text-red-600 dark:text-red-400 mt-0.5 flex-shrink-0" />
                          <p className="text-sm text-red-700 dark:text-red-300">
                            {inviteError}
                          </p>
                        </div>
                      </div>
                    )}

                    <div className="flex justify-end gap-2 mt-6">
                      <button
                        onClick={handleCloseInviteDialog}
                        disabled={inviteLoading}
                        className="px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md disabled:opacity-50"
                      >
                        {tCommon("cancel")}
                      </button>
                      <button
                        onClick={handleCreateInvitation}
                        disabled={
                          inviteLoading ||
                          ((inviteRole === "member" ||
                            inviteRole === "viewer") &&
                            contexts.filter((c) => !c.is_private).length === 0)
                        }
                        className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
                      >
                        {inviteLoading && <InlineSpinner size="sm" />}
                        {inviteLoading ? t("creating") : t("createInvitation")}
                      </button>
                    </div>
                  </>
                )}
              </div>
            ) : (
              // Step 2: Show created invitation URL
              <div className="space-y-4">
                <div className="bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg p-4">
                  <p className="text-sm text-green-800 dark:text-green-200 mb-2">
                    ✓ {t("invitationSuccess")}
                  </p>
                  <p className="text-xs text-green-700 dark:text-green-300">
                    {t("shareInviteLink")}
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium mb-1 text-gray-700 dark:text-gray-300">
                    {t("invitationLink")}
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={createdInviteUrl}
                      readOnly
                      className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-gray-50 dark:bg-gray-700 text-gray-900 dark:text-gray-100 text-sm"
                    />
                    <button
                      onClick={() =>
                        handleCopyInviteUrl(createdInviteUrl, createdInviteUrl)
                      }
                      className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 flex items-center gap-2"
                    >
                      {copiedToken === createdInviteUrl ? (
                        <>
                          <Check className="w-4 h-4" />
                          {t("copied")}
                        </>
                      ) : (
                        <>
                          <Copy className="w-4 h-4" />
                          {tCommon("copy")}
                        </>
                      )}
                    </button>
                  </div>
                </div>

                <p className="text-xs text-gray-600 dark:text-gray-400">
                  {t("inviteLinkAddedNote")}
                </p>

                <div className="flex justify-end mt-6">
                  <button
                    onClick={handleCloseInviteDialog}
                    className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700"
                  >
                    {tCommon("close")}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Upgrade to Pro Prompt (Issue #165) */}
      {showUpgradePrompt && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold mb-4 text-gray-900 dark:text-gray-100">
              {t("proPlanRequiredTitle")}
            </h3>

            <p className="text-gray-600 dark:text-gray-400 mb-6">
              {t("proPlanRequiredDesc")}
            </p>

            <div className="bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800 rounded-lg p-4 mb-6">
              <p className="text-sm text-purple-900 dark:text-purple-100 font-medium mb-2">
                {t("proPlanBenefits")}
              </p>
              <ul className="text-sm text-purple-800 dark:text-purple-200 space-y-1">
                <li>✓ {t("benefitTeamInvitations")}</li>
                <li>✓ {t("benefitSharedContexts")}</li>
                <li>✓ {t("benefitCollaboration")}</li>
              </ul>
            </div>

            <div className="flex gap-2">
              <button
                onClick={() => setShowUpgradePrompt(false)}
                className="flex-1 px-4 py-2 text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-md"
              >
                {tCommon("cancel")}
              </button>
              <button
                onClick={() => {
                  setShowUpgradePrompt(false);
                  window.location.href = "/workspace/plan";
                }}
                className="flex-1 px-4 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700"
              >
                {t("upgradeToPro")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Issue #217: Delete Member Confirmation Dialog */}
      <AlertDialog
        open={showDeleteMemberDialog}
        onOpenChange={setShowDeleteMemberDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-red-500" />
              {t("removeMemberTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("removeMemberDesc", {
                name:
                  memberToDelete?.user_name ||
                  memberToDelete?.user_email ||
                  "this member",
              })}
            </AlertDialogDescription>
            <div className="mt-4 p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
              <p className="text-sm text-amber-800 dark:text-amber-200">
                {t("removeMemberWarning")}
              </p>
            </div>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteLoading}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmRemoveMember}
              disabled={deleteLoading}
              className="bg-red-600 hover:bg-red-700 focus:ring-red-600"
            >
              {deleteLoading ? (
                <>
                  <InlineSpinner size="sm" className="mr-2" />
                  {t("removing")}
                </>
              ) : (
                t("removeMember")
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Issue #217: Delete Invitation Confirmation Dialog */}
      <AlertDialog
        open={showDeleteInvitationDialog}
        onOpenChange={setShowDeleteInvitationDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-amber-500" />
              {t("revokeInvitationTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("revokeInvitationDesc", {
                email: invitationToDelete?.email || "this user",
              })}
            </AlertDialogDescription>
            <div className="mt-4 p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
              <p className="text-sm text-amber-800 dark:text-amber-200">
                {t("revokeInvitationWarning")}
              </p>
            </div>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteInvitationLoading}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDeleteInvitation}
              disabled={deleteInvitationLoading}
              className="bg-amber-600 hover:bg-amber-700 focus:ring-amber-600"
            >
              {deleteInvitationLoading ? (
                <>
                  <InlineSpinner size="sm" className="mr-2" />
                  {t("revoking")}
                </>
              ) : (
                t("revokeInvitation")
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Issue #234: Role Change Confirmation Dialog */}
      <AlertDialog
        open={showRoleChangeDialog}
        onOpenChange={setShowRoleChangeDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("roleChangeConfirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {pendingRoleChange &&
                t("roleChangeConfirmDesc", {
                  name:
                    pendingRoleChange.member.user_name ||
                    pendingRoleChange.member.user_email ||
                    "User",
                })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="py-4">
            <p className="text-sm text-amber-600 dark:text-amber-400">
              {t("roleChangeWarning")}
            </p>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel
              disabled={roleChangeLoading}
              onClick={() => {
                setShowRoleChangeDialog(false);
                setPendingRoleChange(null);
              }}
            >
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingRoleChange) {
                  executeRoleChange(
                    pendingRoleChange.member.user_id,
                    pendingRoleChange.newRole,
                  );
                }
              }}
              disabled={roleChangeLoading}
              className="bg-blue-600 hover:bg-blue-700 focus:ring-blue-600"
            >
              {roleChangeLoading ? (
                <>
                  <InlineSpinner size="sm" className="mr-2" />
                  {tCommon("loading")}
                </>
              ) : (
                t("confirmRoleChange")
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Issue #234: Context Access Dialog */}
      <AlertDialog
        open={showContextAccessDialog}
        onOpenChange={setShowContextAccessDialog}
      >
        <AlertDialogContent className="max-w-lg">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <Settings className="h-5 w-5 text-blue-500" />
              {t("editContextAccess")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("contextAccessDesc")}
            </AlertDialogDescription>
          </AlertDialogHeader>

          <div className="py-4 space-y-4">
            {/* Info message */}
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {t("contextAccessInfo")}
            </p>

            {/* Select All / Deselect All buttons */}
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  const allIds = contexts.map((c) => c.id);
                  setSelectedContextIds(allIds);
                }}
                disabled={contexts.length === 0}
              >
                {t("selectAll")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setSelectedContextIds([])}
                disabled={selectedContextIds.length === 0}
              >
                {t("deselectAll")}
              </Button>
            </div>

            {/* Context selection */}
            <div className="border border-gray-200 dark:border-gray-700 rounded-lg max-h-60 overflow-y-auto">
              {contexts.length === 0 ? (
                <div className="p-4 text-center text-gray-500 text-sm">
                  {t("noContextsAvailable")}
                </div>
              ) : (
                <ul className="divide-y divide-gray-200 dark:divide-gray-700">
                  {contexts.map((context) => (
                    <li
                      key={context.id}
                      className="px-4 py-2 hover:bg-gray-50 dark:hover:bg-gray-800"
                    >
                      <label className="flex items-center gap-3 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={selectedContextIds.includes(context.id)}
                          onChange={() => toggleContextSelection(context.id)}
                          className="w-4 h-4 text-blue-600 rounded"
                        />
                        <div>
                          <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                            {context.display_name || context.name}
                          </div>
                          {context.summary && (
                            <div className="text-xs text-gray-500 dark:text-gray-400 truncate max-w-xs">
                              {context.summary}
                            </div>
                          )}
                        </div>
                      </label>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Status message */}
            {selectedContextIds.length === 0 ? (
              <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-3">
                <p className="text-sm text-red-800 dark:text-red-200 font-medium">
                  ⚠️ {t("inviteContextRequired")}
                </p>
              </div>
            ) : (
              <p className="text-sm text-gray-600 dark:text-gray-400">
                {t("contextsSelected", { count: selectedContextIds.length })}
              </p>
            )}
          </div>

          <AlertDialogFooter>
            <AlertDialogCancel disabled={contextAccessLoading}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleSaveContextAccess}
              disabled={contextAccessLoading || selectedContextIds.length === 0}
              className="bg-blue-600 hover:bg-blue-700 focus:ring-blue-600"
            >
              {contextAccessLoading ? (
                <>
                  <InlineSpinner size="sm" className="mr-2" />
                  {tCommon("saving")}
                </>
              ) : (
                t("saveContextAccess")
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageContainer>
  );
}

interface PermissionRowProps {
  permission: string;
  owner: boolean;
  admin: boolean;
  member: boolean;
  viewer: boolean;
}

function PermissionRow({
  permission,
  owner,
  admin,
  member,
  viewer,
}: PermissionRowProps) {
  const CheckIcon = () => (
    <Check className="h-4 w-4 text-green-600 dark:text-green-400 mx-auto" />
  );
  const EmptyIcon = () => (
    <span className="text-gray-300 dark:text-gray-600 mx-auto">-</span>
  );

  return (
    <tr className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
      <td className="px-4 py-3 text-sm text-gray-900 dark:text-gray-100">
        {permission}
      </td>
      <td className="px-4 py-3 text-center">
        {owner ? <CheckIcon /> : <EmptyIcon />}
      </td>
      <td className="px-4 py-3 text-center">
        {admin ? <CheckIcon /> : <EmptyIcon />}
      </td>
      <td className="px-4 py-3 text-center">
        {member ? <CheckIcon /> : <EmptyIcon />}
      </td>
      <td className="px-4 py-3 text-center">
        {viewer ? <CheckIcon /> : <EmptyIcon />}
      </td>
    </tr>
  );
}
