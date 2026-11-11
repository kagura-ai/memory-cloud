"use client";

/**
 * Admin Users Management Page
 *
 * Manage all users, view statistics, change roles, and delete users.
 * Admin-only page (Issue #43).
 *
 * #2765: three categories synced to `?tab=` — Users (this list), Plan tiers
 * and Plan changes (the cross-workspace views of the former Quotas page).
 * A workspace's plan, add-ons and spend cap are managed from its owner's
 * user detail.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import { apiClient } from "@/lib/api";
import { adminUserPath } from "@/lib/api/admin";
import { formatRelativeTime } from "@/lib/utils/datetime";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  MoreVertical,
  Shield,
  User as UserIcon,
  Eye,
  Trash2,
  RefreshCw,
  Lock,
  Search,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useTabParam } from "@/hooks/useTabParam";
import {
  CategoryTabs,
  CategoryTabsContent,
  CategoryTabsList,
  CategoryTabsTrigger,
} from "@/components/ui/tabs";
import { PlanTiersTable } from "@/components/admin/plans/PlanTiersTable";
import { PlanAuditTable } from "@/components/admin/plans/PlanAuditTable";
import {
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import {
  UserDeleteDialog,
  type UserDeleteTarget,
} from "@/components/admin/UserDeleteDialog";
import {
  WorkspacePlanBadges,
  type WorkspaceMembership,
} from "./WorkspacePlanBadges";

const USER_TABS = ["users", "plan-tiers", "plan-changes"] as const;

interface User {
  id: string;
  email: string;
  name: string;
  picture?: string;
  role: string;
  is_initial_admin?: boolean; // Issue #166: System Admin protection
  created_at: string;
  last_login?: string;
  memory_count: number;
  is_active: boolean;
  timezone?: string; // Issue #175: User timezone preference
  auth_provider?: string | null; // Issue #361: Registration provider
  workspaces?: WorkspaceMembership[]; // Issue #165: Workspace badges

  // Issue #695: owned-workspace cap summary surfaced on the list page.
  // Field names mirror the detail endpoint's WorkspaceSummary (cap /
  // base_cap) so the same mental model carries across admin endpoints.
  // All three are optional so the cell can still render during a deploy
  // gap; the render path derives a safe fallback from base_cap + bonus
  // rather than a literal 1, so a user with a nonzero bonus shown in the
  // tooltip never gets a denominator that contradicts it.
  owned_count?: number;
  workspace_slot_bonus?: number;
  base_cap?: number;
  cap?: number;

  // Current context info
  current_context_id?: string | null;
  current_context_name?: string | null;
  current_context_display_name?: string | null;
}

export default function AdminUsersPage() {
  const t = useTranslations("admin.users");
  const tCommon = useTranslations("admin.common");
  const locale = useLocale();
  const router = useRouter();

  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();
  const [tab, setTab] = useTabParam(USER_TABS[0], "tab", USER_TABS);

  // Issue #165 Phase 5: Search and filter state
  const [searchQuery, setSearchQuery] = useState("");

  // #2420: the user the deletion dialog is open for.
  const [deleteTarget, setDeleteTarget] = useState<UserDeleteTarget | null>(
    null,
  );

  // Issue #166: Count admin users for protection logic
  const adminCount = users.filter((u) => u.role === "admin").length;

  // Filter users based on search query (Issue #165)
  const filteredUsers = users.filter((user) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      user.email.toLowerCase().includes(query) ||
      user.name.toLowerCase().includes(query) ||
      user.id.toLowerCase().includes(query)
    );
  });

  useEffect(() => {
    loadUsers();
  }, []);

  const loadUsers = async () => {
    try {
      setLoading(true);
      // Issue #165: Include workspace data
      const data = await apiClient.get<{ users: User[]; total: number }>(
        "/api/v1/admin/users?include_workspaces=true",
      );
      setUsers(data.users);
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to load users",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  // Issue #697: navigate to the dedicated detail route instead of opening
  // a legacy modal — the modal predated #676 and lacked the slot-bonus
  // inc/dec UI, leaving the workspace_slot_bonus admin flow unreachable
  // from the standard list-click path.
  const handleUserClick = (userId: string) => {
    router.push(adminUserPath(userId));
  };

  const handleRoleChange = async (userId: string, newRole: string) => {
    try {
      await apiClient.put(`/api/v1/admin/users/${userId}/role`, {
        role: newRole,
      });
      toast({
        title: "Success",
        description: `User role updated to ${newRole}`,
      });
      loadUsers();
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to update user role",
        variant: "destructive",
      });
    }
  };

  // #2420: the dialog confirms, then on the API's 409 asks for a successor
  // for each owned workspace and shared context, and resends the DELETE.
  const handleDeleteUser = (userId: string, userName: string) => {
    setDeleteTarget({ id: userId, name: userName });
  };

  return (
    <PageContainer>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          tab === USER_TABS[0] ? (
            <Button onClick={loadUsers} variant="outline" disabled={loading}>
              {loading ? (
                <InlineSpinner size="sm" className="mr-2" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-2" />
              )}
              {tCommon("refresh")}
            </Button>
          ) : undefined
        }
      />

      <CategoryTabs value={tab} onValueChange={setTab}>
        <CategoryTabsList>
          <CategoryTabsTrigger value={USER_TABS[0]}>
            {t("tabs.users")}
          </CategoryTabsTrigger>
          <CategoryTabsTrigger value={USER_TABS[1]}>
            {t("tabs.planTiers")}
          </CategoryTabsTrigger>
          <CategoryTabsTrigger value={USER_TABS[2]}>
            {t("tabs.planChanges")}
          </CategoryTabsTrigger>
        </CategoryTabsList>

        <CategoryTabsContent value={USER_TABS[0]} helpText={t("tabHelp.users")}>
          {loading ? (
            <TableLoadingState rows={5} />
          ) : (
            <>
              {/* Search Bar (Issue #165 Phase 5) */}
              <Section>
                <div className="relative">
                  <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
                  <Input
                    type="text"
                    placeholder={t("search.placeholder")}
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="pl-10"
                  />
                </div>
                {searchQuery && (
                  <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
                    {t("search.showing", {
                      filtered: filteredUsers.length,
                      total: users.length,
                    })}
                  </p>
                )}
              </Section>

              <Section title={t("overview.title")}>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                  <div className="bg-blue-50 dark:bg-blue-900/20 p-4 rounded-lg">
                    <h3 className="text-sm font-medium text-blue-900 dark:text-blue-100">
                      Total Users
                    </h3>
                    <p className="text-2xl font-bold text-blue-700 dark:text-blue-300">
                      {users.length}
                    </p>
                  </div>
                  <div className="bg-green-50 dark:bg-green-900/20 p-4 rounded-lg">
                    <h3 className="text-sm font-medium text-green-900 dark:text-green-100">
                      Active Users
                    </h3>
                    <p className="text-2xl font-bold text-green-700 dark:text-green-300">
                      {users.filter((u) => u.is_active).length}
                    </p>
                  </div>
                  <div className="bg-purple-50 dark:bg-purple-900/20 p-4 rounded-lg">
                    <h3 className="text-sm font-medium text-purple-900 dark:text-purple-100">
                      Total Memories
                    </h3>
                    <p className="text-2xl font-bold text-purple-700 dark:text-purple-300">
                      {users.reduce((sum, u) => sum + u.memory_count, 0)}
                    </p>
                  </div>
                </div>

                <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t("table.user")}</TableHead>
                        <TableHead>{t("table.role")}</TableHead>
                        <TableHead>{t("table.workspaces")}</TableHead>
                        <TableHead>{t("table.cap")}</TableHead>
                        <TableHead>{t("table.memories")}</TableHead>
                        <TableHead>{t("table.lastLogin")}</TableHead>
                        <TableHead className="text-right">
                          {t("table.actions")}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filteredUsers.map((user) => (
                        <TableRow
                          key={user.id}
                          className="cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                          onClick={(e) => {
                            // Don't trigger if clicking on dropdown
                            if (
                              (e.target as HTMLElement).closest('[role="menu"]')
                            )
                              return;
                            handleUserClick(user.id);
                          }}
                        >
                          <TableCell>
                            <div className="flex items-center gap-3">
                              {user.picture ? (
                                <img
                                  src={user.picture}
                                  alt={user.name}
                                  className="w-8 h-8 rounded-full"
                                />
                              ) : (
                                <div className="w-8 h-8 rounded-full bg-gray-200 flex items-center justify-center">
                                  <UserIcon className="w-4 h-4 text-gray-600" />
                                </div>
                              )}
                              <div>
                                <p className="font-medium text-sm">
                                  {user.name}
                                </p>
                                <div className="flex items-center gap-1">
                                  <span className="text-xs text-gray-500">
                                    {user.email}
                                  </span>
                                  {user.auth_provider && (
                                    <span
                                      className="text-xs text-gray-400 dark:text-gray-500"
                                      title={`Registered via ${user.auth_provider}`}
                                    >
                                      (
                                      {user.auth_provider === "github"
                                        ? "GitHub"
                                        : user.auth_provider
                                            .charAt(0)
                                            .toUpperCase() +
                                          user.auth_provider.slice(1)}
                                      )
                                    </span>
                                  )}
                                </div>
                              </div>
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-2">
                              {user.role === "admin" ? (
                                <Badge className="bg-red-100 text-red-800">
                                  <Shield className="h-3 w-3 mr-1" />
                                  System Admin
                                </Badge>
                              ) : (
                                <Badge className="bg-blue-100 text-blue-800">
                                  User
                                </Badge>
                              )}
                              {user.is_initial_admin && (
                                <span
                                  className="text-xs text-amber-600"
                                  title="Initial system administrator (cannot be deleted)"
                                >
                                  <Lock className="inline h-3 w-3" />
                                </span>
                              )}
                            </div>
                          </TableCell>
                          <TableCell>
                            {/* Issue #165: Workspace badges; #1754: with each plan */}
                            {user.workspaces && user.workspaces.length > 0 ? (
                              <WorkspacePlanBadges
                                workspaces={user.workspaces}
                              />
                            ) : (
                              <span className="text-xs text-gray-400 dark:text-gray-500">
                                {t("table.none")}
                              </span>
                            )}
                          </TableCell>
                          <TableCell>
                            {/* Issue #695: owned / cap with at-cap emphasis.
                        Fallback derives cap as base_cap + bonus rather
                        than a literal 1 — during a mixed-version deploy
                        (frontend ahead of backend) a user with bonus > 0
                        would otherwise see "owned / 1" in the denominator
                        even though the tooltip shows a nonzero bonus. */}
                            {(() => {
                              const owned = user.owned_count ?? 0;
                              const bonus = user.workspace_slot_bonus ?? 0;
                              const baseCap = user.base_cap ?? 1;
                              const cap = user.cap ?? baseCap + bonus;
                              const atCap = owned >= cap;
                              return (
                                <span
                                  className={
                                    atCap
                                      ? "text-amber-700 dark:text-amber-400 font-medium tabular-nums"
                                      : "tabular-nums text-sm text-gray-700 dark:text-gray-300"
                                  }
                                  title={t("table.capTooltip", {
                                    owned,
                                    cap,
                                    bonus,
                                  })}
                                >
                                  {owned} / {cap}
                                </span>
                              );
                            })()}
                          </TableCell>
                          <TableCell>{user.memory_count}</TableCell>
                          <TableCell className="text-sm text-gray-500">
                            {user.last_login
                              ? formatRelativeTime(user.last_login, locale)
                              : "Never"}
                          </TableCell>
                          <TableCell className="text-right">
                            <DropdownMenu>
                              <DropdownMenuTrigger asChild>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  aria-label="Row actions"
                                  title="Row actions"
                                  onClick={(e) => e.stopPropagation()}
                                >
                                  <MoreVertical className="h-4 w-4" />
                                </Button>
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="end">
                                <DropdownMenuItem
                                  onClick={() =>
                                    handleRoleChange(
                                      user.id,
                                      user.role === "admin" ? "user" : "admin",
                                    )
                                  }
                                  disabled={
                                    user.is_initial_admin ||
                                    (user.role === "admin" && adminCount <= 1)
                                  }
                                >
                                  <Shield className="mr-2 h-4 w-4" />
                                  {user.role === "admin"
                                    ? "Demote from System Admin"
                                    : "Promote to System Admin"}
                                </DropdownMenuItem>
                                <DropdownMenuItem
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    handleUserClick(user.id);
                                  }}
                                >
                                  <Eye className="mr-2 h-4 w-4" />
                                  View Details
                                </DropdownMenuItem>
                                <DropdownMenuItem
                                  onClick={() =>
                                    handleDeleteUser(user.id, user.name)
                                  }
                                  className="text-red-600"
                                  disabled={
                                    user.is_initial_admin ||
                                    (user.role === "admin" && adminCount <= 1)
                                  }
                                >
                                  <Trash2 className="mr-2 h-4 w-4" />
                                  Delete User
                                  {user.is_initial_admin && " (Protected)"}
                                </DropdownMenuItem>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              </Section>
            </>
          )}
        </CategoryTabsContent>

        <CategoryTabsContent
          value={USER_TABS[1]}
          helpText={t("tabHelp.planTiers")}
        >
          <PlanTiersTable />
        </CategoryTabsContent>

        <CategoryTabsContent
          value={USER_TABS[2]}
          helpText={t("tabHelp.planChanges")}
        >
          <PlanAuditTable />
        </CategoryTabsContent>
      </CategoryTabs>
      {deleteTarget && (
        <UserDeleteDialog
          key={deleteTarget.id}
          user={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => {
            setDeleteTarget(null);
            loadUsers();
          }}
        />
      )}
    </PageContainer>
  );
}
