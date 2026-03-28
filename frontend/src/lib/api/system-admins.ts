/**
 * System Admin Management API Client
 *
 * Issue #166: System Admin vs Workspace Admin RBAC separation
 *
 * Provides functions to:
 * - List all system administrators
 * - Promote users to system admin
 * - Demote system admins (with protection)
 */

import { apiClient } from './index';

/**
 * System Administrator with flags and statistics
 */
export interface SystemAdmin {
  id: number;
  email: string;
  user_id: string;
  name: string;
  picture?: string;
  role: string; // 'admin' or 'user'
  is_initial_admin: boolean;
  created_at: string;
  last_login_at?: string;
  memory_count: number;
  is_active: boolean;
}

/**
 * Response for listing system administrators
 */
export interface SystemAdminListResponse {
  admins: SystemAdmin[];
  total: number;
  initial_admin_id: number;
}

/**
 * Request to promote user to system admin
 */
export interface PromoteToSystemAdminRequest {
  user_id: string;
}

/**
 * Response for system admin promotion
 */
export interface PromoteToSystemAdminResponse {
  success: boolean;
  user: SystemAdmin;
  message: string;
}

/**
 * Response for system admin demotion
 */
export interface DemoteSystemAdminResponse {
  success: boolean;
  message: string;
}

/**
 * List all system administrators
 *
 * Requires: System Admin role
 *
 * @returns List of system admins with statistics
 * @throws Error if request fails or user is not authorized
 *
 * @example
 * ```typescript
 * const { admins, total, initial_admin_id } = await listSystemAdmins();
 * console.log(`Found ${total} system admins`);
 * console.log(`Initial admin ID: ${initial_admin_id}`);
 * ```
 */
export async function listSystemAdmins(): Promise<SystemAdminListResponse> {
  const response = await apiClient.get<SystemAdminListResponse>(
    '/api/v1/admin/system-admins'
  );
  return response;
}

/**
 * Promote user to system administrator
 *
 * Requires: System Admin role
 *
 * @param userId - OAuth2 user_id to promote
 * @returns Promotion result with updated user info
 * @throws Error if user not found or already admin
 *
 * @example
 * ```typescript
 * const result = await promoteToSystemAdmin('google-oauth2|123456');
 * console.log(result.message); // "User admin2@example.com promoted to system administrator"
 * ```
 */
export async function promoteToSystemAdmin(
  userId: string
): Promise<PromoteToSystemAdminResponse> {
  const response = await apiClient.post<PromoteToSystemAdminResponse>(
    '/api/v1/admin/system-admins',
    { user_id: userId }
  );
  return response;
}

/**
 * Demote system administrator to regular user
 *
 * Protection:
 * - Cannot demote initial admin (is_initial_admin=True)
 * - Cannot demote last remaining admin
 *
 * Requires: System Admin role
 *
 * @param userId - OAuth2 user_id to demote
 * @returns Demotion result
 * @throws Error if user not found, not admin, or operation is protected
 *
 * @example
 * ```typescript
 * try {
 *   const result = await demoteSystemAdmin('google-oauth2|123456');
 *   console.log(result.message); // "User admin2@example.com demoted to standard user"
 * } catch (error) {
 *   // Handle protection errors (initial admin or last admin)
 *   console.error(error);
 * }
 * ```
 */
export async function demoteSystemAdmin(
  userId: string
): Promise<DemoteSystemAdminResponse> {
  const response = await apiClient.delete<DemoteSystemAdminResponse>(
    `/api/v1/admin/system-admins/${userId}`
  );
  return response;
}
