"use client";

/**
 * Admin Users Management Page
 *
 * Manage all users, view statistics, change roles, and delete users.
 * Admin-only page (Issue #43).
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { PageContainer } from "@/components/common/PageContainer";
import { Section } from "@/components/common/Section";
import { apiClient } from "@/lib/api";
import { formatRelativeTime } from "@/lib/utils/datetime";
import { useAuth } from "@/contexts/AuthContext";
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
  Building2,
  CreditCard,
  Search,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import {
  InlineSpinner,
  TableLoadingState,
} from "@/components/common/LoadingState";
import { UserDetailModal } from "@/components/admin/UserDetailModal";

interface WorkspaceMembership {
  workspace_id: string;
  workspace_name: string;
  role: string;
  plan_name: string;
}

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

  // Current context info
  current_context_id?: string | null;
  current_context_name?: string | null;
  current_context_display_name?: string | null;
}

export default function AdminUsersPage() {
  const t = useTranslations("admin.users");
  const tCommon = useTranslations("admin.common");

  const { user } = useAuth();
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedUser, setSelectedUser] = useState<User | null>(null);
  const { toast } = useToast();
  const router = useRouter(); // Issue #164: Navigation to user detail

  // Issue #165 Phase 5: Search and filter state
  const [searchQuery, setSearchQuery] = useState("");
  const [showDetailModal, setShowDetailModal] = useState(false);
  const [detailUserId, setDetailUserId] = useState<string | null>(null);

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

  const handleUserClick = (userId: string) => {
    setDetailUserId(userId);
    setShowDetailModal(true);
  };

  const handleCloseDetailModal = () => {
    setShowDetailModal(false);
    setDetailUserId(null);
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

  const handleDeleteUser = async (userId: string, userName: string) => {
    if (
      !confirm(
        `Delete user ${userName}? This will permanently delete all their memories.`,
      )
    ) {
      return;
    }

    try {
      await apiClient.delete(`/api/v1/admin/users/${userId}`);
      toast({
        title: "Success",
        description: `User ${userName} deleted successfully`,
      });
      loadUsers();
    } catch (error) {
      toast({
        title: "Error",
        description: "Failed to delete user",
        variant: "destructive",
      });
    }
  };

  const getRoleBadgeColor = (role: string) => {
    switch (role) {
      case "admin":
        return "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300";
      case "user":
        return "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300";
      default:
        return "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300";
    }
  };

  if (loading) {
    return (
      <PageContainer>
        <PageHeader
          title={t("title")}
          description={t("description")}
          actions={
            <Button onClick={loadUsers} variant="outline" disabled={loading}>
              {loading ? (
                <InlineSpinner size="sm" className="mr-2" />
              ) : (
                <RefreshCw className="h-4 w-4 mr-2" />
              )}
              {tCommon("refresh")}
            </Button>
          }
        />
        <TableLoadingState rows={5} />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={t("title")}
        description={t("description")}
        actions={
          <Button onClick={loadUsers} variant="outline" disabled={loading}>
            {loading ? (
              <InlineSpinner size="sm" className="mr-2" />
            ) : (
              <RefreshCw className="h-4 w-4 mr-2" />
            )}
            {tCommon("refresh")}
          </Button>
        }
      />

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
                <TableHead>{t("table.memories")}</TableHead>
                <TableHead>{t("table.lastLogin")}</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredUsers.map((user) => (
                <TableRow
                  key={user.id}
                  className="cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                  onClick={(e) => {
                    // Don't trigger if clicking on dropdown
                    if ((e.target as HTMLElement).closest('[role="menu"]'))
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
                        <p className="font-medium text-sm">{user.name}</p>
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
                                : user.auth_provider.charAt(0).toUpperCase() +
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
                    {/* Issue #165: Workspace badges */}
                    {user.workspaces && user.workspaces.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {user.workspaces.slice(0, 2).map((workspace) => (
                          <Badge
                            key={workspace.workspace_id}
                            className="bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200 text-xs"
                            title={`${workspace.workspace_name} (${workspace.role})`}
                          >
                            <Building2 className="h-3 w-3 mr-1" />
                            {workspace.workspace_name}
                          </Badge>
                        ))}
                        {user.workspaces.length > 2 && (
                          <Badge className="bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300 text-xs">
                            +{user.workspaces.length - 2}
                          </Badge>
                        )}
                      </div>
                    ) : (
                      <span className="text-xs text-gray-400 dark:text-gray-500">
                        None
                      </span>
                    )}
                  </TableCell>
                  <TableCell>{user.memory_count}</TableCell>
                  <TableCell className="text-sm text-gray-500">
                    {user.last_login
                      ? formatRelativeTime(user.last_login, user.timezone)
                      : "Never"}
                  </TableCell>
                  <TableCell className="text-right">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="sm">
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
                          onClick={() => handleDeleteUser(user.id, user.name)}
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

      {/* User Detail Modal (Issue #165 Phase 5) */}
      {showDetailModal && detailUserId && (
        <UserDetailModal
          userId={detailUserId}
          onClose={handleCloseDetailModal}
        />
      )}
    </PageContainer>
  );
}
