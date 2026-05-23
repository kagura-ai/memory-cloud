/**
 * Role-Based Access Control (RBAC) Utilities
 *
 * Provides role checking and permission utilities for the Web UI.
 * Issue #664: Web UI Redesign Phase 1
 * Issue #166: System Admin vs Workspace Admin RBAC separation
 */

import { User } from "./auth";

/**
 * System-level user roles
 *
 * Note: Workspace-level roles (owner/admin/member/viewer) are separate
 * and managed in WorkspaceMember.role
 */
export enum Role {
  ADMIN = "admin", // System Administrator (platform-wide)
  USER = "user", // Standard user
}

/**
 * Role hierarchy: higher number = more permissions
 */
const ROLE_HIERARCHY: Record<Role, number> = {
  [Role.ADMIN]: 2,
  [Role.USER]: 1,
};

/**
 * Check if user has a specific role
 */
export function hasRole(user: User | null | undefined, role: Role): boolean {
  if (!user || !user.role) {
    return false;
  }

  const userRoleLevel = ROLE_HIERARCHY[user.role as Role] ?? 0;
  const requiredRoleLevel = ROLE_HIERARCHY[role];

  return userRoleLevel >= requiredRoleLevel;
}

/**
 * Check if user is admin
 */
export function isAdmin(user: User | null | undefined): boolean {
  return hasRole(user, Role.ADMIN);
}

/**
 * Check if user can edit/modify resources
 */
export function canEdit(user: User | null | undefined): boolean {
  return hasRole(user, Role.USER);
}

/**
 * Get user role label for display
 */
export function getRoleLabel(role: string | undefined): string {
  switch (role) {
    case Role.ADMIN:
      return "System Administrator";
    case Role.USER:
      return "User";
    default:
      return "Unknown";
  }
}

/**
 * Get role badge color (for UI display)
 */
export function getRoleBadgeColor(
  role: string | undefined,
): "default" | "destructive" | "secondary" | "outline" {
  switch (role) {
    case Role.ADMIN:
      return "destructive"; // Red for system admin
    case Role.USER:
      return "default"; // Default color
    default:
      return "outline";
  }
}

// ============================================================================
// System Admin vs Workspace Admin Utilities (Issue #166)
// ============================================================================

/**
 * Check if user is a system administrator
 *
 * System admins have platform-wide access, independent of workspace roles.
 */
export function isSystemAdmin(user: User | null | undefined): boolean {
  return user?.role === "admin";
}

/**
 * Workspace-level tenancy role (mirrors backend WorkspaceRole StrEnum).
 *
 * Issue #700: Replaces stringly-typed role literals across the frontend.
 * Values must match the Python enum byte-for-byte (no codegen, manual sync).
 */
export enum WorkspaceRole {
  Owner = "owner",
  Admin = "admin",
  Member = "member",
  Viewer = "viewer",
}

/**
 * Context-level tenancy role (mirrors backend ContextRole StrEnum).
 */
export enum ContextRole {
  Owner = "owner",
  Editor = "editor",
  Viewer = "viewer",
}

/**
 * Workspace role hierarchy weights (higher = more privilege)
 */
const WORKSPACE_ROLE_WEIGHTS: Record<WorkspaceRole, number> = {
  [WorkspaceRole.Owner]: 4,
  [WorkspaceRole.Admin]: 3,
  [WorkspaceRole.Member]: 2,
  [WorkspaceRole.Viewer]: 1,
};

/**
 * Check if user's workspace role meets the minimum required role.
 *
 * Issue #59: Unified workspace role hierarchy check.
 */
export function hasWorkspaceRole(
  userRole: WorkspaceRole | string | null | undefined,
  requiredRole: Exclude<WorkspaceRole, WorkspaceRole.Viewer>,
): boolean {
  if (!userRole) return false;
  const userWeight = WORKSPACE_ROLE_WEIGHTS[userRole as WorkspaceRole] ?? 0;
  return userWeight >= WORKSPACE_ROLE_WEIGHTS[requiredRole];
}

/**
 * Check if user is an workspace administrator
 *
 * Workspace admins (owner or admin role at workspace level) have workspace-wide access
 * but not system-level access.
 */
export function isWorkspaceAdmin(
  workspaceRole: WorkspaceRole | string | undefined,
): boolean {
  return (
    workspaceRole === WorkspaceRole.Admin ||
    workspaceRole === WorkspaceRole.Owner
  );
}

/**
 * Get system role badge color
 *
 * Returns Tailwind color classes for system-level role badges.
 */
export function getSystemRoleBadgeColor(role: string | undefined): string {
  switch (role) {
    case "admin":
      return "bg-red-100 text-red-800"; // Red for system admin
    case "user":
      return "bg-blue-100 text-blue-800"; // Blue for user
    default:
      return "bg-gray-100 text-gray-800";
  }
}

/**
 * Get workspace role badge color
 *
 * Returns Tailwind color classes for workspace-level role badges.
 */
export function getWorkspaceRoleBadgeColor(
  workspaceRole: WorkspaceRole | string | undefined,
): string {
  switch (workspaceRole) {
    case WorkspaceRole.Owner:
      return "bg-purple-100 text-purple-800"; // Purple for owner
    case WorkspaceRole.Admin:
      return "bg-blue-100 text-blue-800"; // Blue for workspace admin
    case WorkspaceRole.Member:
      return "bg-gray-100 text-gray-800"; // Gray for member
    case WorkspaceRole.Viewer:
      return "bg-green-100 text-green-800"; // Green for viewer
    default:
      return "bg-gray-100 text-gray-800";
  }
}
