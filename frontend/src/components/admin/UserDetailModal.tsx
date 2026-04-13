"use client";

/**
 * User Detail Modal
 *
 * Issue #165 Phase 5: User Management UI Extension
 *
 * Comprehensive user detail view showing:
 * - Basic user information
 * - Workspace memberships with roles
 * - Accessible contexts
 * - Usage statistics
 */

import { useEffect, useState } from "react";
import {
  X,
  Shield,
  Lock,
  Building2,
  FolderOpen,
  Database,
  TrendingUp,
} from "lucide-react";
import { getUserDetails, UserDetail } from "@/lib/api/admin";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { Badge } from "@/components/ui/badge";
import { useAuth } from "@/contexts/AuthContext";
import { formatRelativeTime, formatDate } from "@/lib/utils/datetime";

interface UserDetailModalProps {
  userId: string;
  onClose: () => void;
}

export function UserDetailModal({ userId, onClose }: UserDetailModalProps) {
  const { user } = useAuth();
  const [details, setDetails] = useState<UserDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadDetails();
  }, [userId]);

  const loadDetails = async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await getUserDetails(userId);
      setDetails(data);
    } catch (err: unknown) {
      console.error("Failed to load user details:", err);
      setError(
        err instanceof Error ? err.message : "Failed to load user details",
      );
    } finally {
      setLoading(false);
    }
  };

  const getRoleBadgeClass = (role: string) => {
    switch (role) {
      case "admin":
        return "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200";
      case "user":
        return "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200";
      case "owner":
        return "bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200";
      case "member":
        return "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200";
      case "viewer":
        return "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200";
      case "editor":
        return "bg-indigo-100 text-indigo-800 dark:bg-indigo-900 dark:text-indigo-200";
      default:
        return "bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200";
    }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
        <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-2xl w-full mx-4">
          <SpinnerLoading message="Loading user details..." />
        </div>
      </div>
    );
  }

  if (error || !details) {
    return (
      <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
        <div className="bg-white dark:bg-gray-800 rounded-lg p-6 max-w-2xl w-full mx-4">
          <div className="text-center text-red-600 dark:text-red-400">
            {error || "Failed to load user details"}
          </div>
          <button
            onClick={onClose}
            className="mt-4 w-full px-4 py-2 bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-md hover:bg-gray-300 dark:hover:bg-gray-600"
          >
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-gray-800 rounded-lg max-w-5xl w-full max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="sticky top-0 bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700 px-6 py-4 flex justify-between items-start">
          <div>
            <h2 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              {details.user.name}
            </h2>
            <p className="text-gray-600 dark:text-gray-400">
              {details.user.email}
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          >
            <X className="h-6 w-6" />
          </button>
        </div>

        <div className="p-6 space-y-6">
          {/* Basic Information */}
          <section>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3">
              Basic Information
            </h3>
            <dl className="grid grid-cols-2 gap-4">
              <div className="bg-gray-50 dark:bg-gray-900 rounded-lg p-3">
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  User ID
                </dt>
                <dd className="text-sm text-gray-900 dark:text-gray-100 mt-1 font-mono">
                  {details.user.id || details.user.user_id}
                </dd>
              </div>
              <div className="bg-gray-50 dark:bg-gray-900 rounded-lg p-3">
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  System Role
                </dt>
                <dd className="mt-1">
                  <div className="flex items-center gap-2">
                    <Badge className={getRoleBadgeClass(details.user.role)}>
                      {details.user.role === "admin" && (
                        <Shield className="h-3 w-3 mr-1" />
                      )}
                      {details.user.role}
                    </Badge>
                    {details.user.is_initial_admin && (
                      <span className="text-xs text-amber-600 dark:text-amber-400 flex items-center gap-1">
                        <Lock className="h-3 w-3" />
                        Initial Admin
                      </span>
                    )}
                  </div>
                </dd>
              </div>
              <div className="bg-gray-50 dark:bg-gray-900 rounded-lg p-3">
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Created
                </dt>
                <dd className="text-sm text-gray-900 dark:text-gray-100 mt-1">
                  {formatDate(details.user.created_at, user?.timezone)}
                </dd>
              </div>
              <div className="bg-gray-50 dark:bg-gray-900 rounded-lg p-3">
                <dt className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Last Login
                </dt>
                <dd className="text-sm text-gray-900 dark:text-gray-100 mt-1">
                  {details.user.last_login_at
                    ? formatRelativeTime(
                        details.user.last_login_at,
                        user?.timezone,
                      )
                    : "Never"}
                </dd>
              </div>
            </dl>
          </section>

          {/* Workspaces */}
          <section>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3 flex items-center gap-2">
              <Building2 className="h-5 w-5" />
              Workspaces ({details.workspaces.length})
            </h3>
            {details.workspaces.length === 0 ? (
              <div className="text-center py-8 text-gray-500 dark:text-gray-400">
                <Building2 className="h-12 w-12 mx-auto mb-2 opacity-50" />
                <p>Not a member of any workspace</p>
              </div>
            ) : (
              <div className="space-y-2">
                {details.workspaces.map((workspace) => (
                  <div
                    key={workspace.workspace_id}
                    className="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 flex justify-between items-start"
                  >
                    <div className="flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <p className="font-medium text-gray-900 dark:text-gray-100">
                          {workspace.workspace_name}
                        </p>
                        {workspace.is_primary && (
                          <Badge className="bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 text-xs">
                            Current
                          </Badge>
                        )}
                      </div>
                      <div className="flex items-center gap-3 mt-2">
                        <Badge className={getRoleBadgeClass(workspace.role)}>
                          {workspace.role}
                        </Badge>
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          Plan: {workspace.plan_name}
                        </span>
                      </div>
                    </div>
                    {workspace.joined_at && (
                      <div className="text-xs text-gray-500 dark:text-gray-400">
                        Joined {formatDate(workspace.joined_at, user?.timezone)}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Accessible Contexts */}
          <section>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3 flex items-center gap-2">
              <FolderOpen className="h-5 w-5" />
              Accessible Contexts ({details.accessible_contexts.length})
            </h3>
            {details.accessible_contexts.length === 0 ? (
              <div className="text-center py-8 text-gray-500 dark:text-gray-400">
                <FolderOpen className="h-12 w-12 mx-auto mb-2 opacity-50" />
                <p>No accessible contexts</p>
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {details.accessible_contexts.map((ctx) => (
                  <div
                    key={ctx.context_id}
                    className="bg-gray-50 dark:bg-gray-900 rounded-lg p-3 border border-gray-200 dark:border-gray-700"
                  >
                    <div className="flex justify-between items-start mb-2">
                      <div>
                        <p className="font-medium text-gray-900 dark:text-gray-100 text-sm">
                          {ctx.context_name}
                        </p>
                        <p className="text-xs text-gray-600 dark:text-gray-400">
                          {ctx.workspace_name}
                        </p>
                      </div>
                      <Badge className={getRoleBadgeClass(ctx.role)}>
                        {ctx.role}
                      </Badge>
                    </div>
                    {ctx.last_used_at && (
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        Last used{" "}
                        {formatRelativeTime(ctx.last_used_at, user?.timezone)}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Usage Statistics */}
          <section>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3 flex items-center gap-2">
              <TrendingUp className="h-5 w-5" />
              Usage Statistics
            </h3>
            <div className="grid grid-cols-2 gap-4">
              <div className="bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg p-4">
                <div className="flex items-center gap-2 mb-1">
                  <Database className="h-4 w-4 text-blue-600 dark:text-blue-400" />
                  <dt className="text-sm font-medium text-blue-700 dark:text-blue-300">
                    Total Memories
                  </dt>
                </div>
                <dd className="text-2xl font-bold text-blue-900 dark:text-blue-100">
                  {details.stats.total_memories}
                </dd>
                <div className="text-xs text-blue-600 dark:text-blue-400 mt-1">
                  Working: {details.stats.working_memories} | Persistent:{" "}
                  {details.stats.persistent_memories}
                </div>
              </div>

              <div className="bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800 rounded-lg p-4">
                <div className="flex items-center gap-2 mb-1">
                  <TrendingUp className="h-4 w-4 text-purple-600 dark:text-purple-400" />
                  <dt className="text-sm font-medium text-purple-700 dark:text-purple-300">
                    Active API Keys
                  </dt>
                </div>
                <dd className="text-2xl font-bold text-purple-900 dark:text-purple-100">
                  {details.stats.active_api_keys || 0}
                </dd>
              </div>
            </div>
          </section>

          {/* Close Button */}
          <div className="flex justify-end pt-4 border-t border-gray-200 dark:border-gray-700">
            <button
              onClick={onClose}
              className="px-6 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 transition-colors"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
