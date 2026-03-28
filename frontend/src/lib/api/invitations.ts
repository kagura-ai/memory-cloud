/**
 * Workspace Invitations API Client
 *
 * Issue #165: Team Collaboration Features - Workspace Invitation System
 */

import { apiClient } from './base';
import type { Workspace, WorkspaceMember } from './workspaces';

export interface WorkspaceInvitation {
  id: number;
  workspace_id: string;
  token: string;
  email: string | null;
  role: 'owner' | 'admin' | 'member' | 'viewer';
  invited_by: string;
  expires_at: string | null;
  accepted_at: string | null;
  accepted_by: string | null;
  created_at: string;
  invitation_url: string;
  is_expired: boolean;
  is_accepted: boolean;
  allowed_context_ids: string[] | null; // Migration 042: Context access restriction
}

export interface CreateInvitationRequest {
  email?: string;
  role: 'owner' | 'admin' | 'member' | 'viewer';
  expires_in_days?: number | null; // 7, 30, 90, 365, or null = never
  allowed_context_ids?: string[] | null; // Migration 042: Context selection (required for member/viewer)
}

export interface AcceptInvitationRequest {
  token: string;
}

export interface AcceptInvitationResponse {
  success: boolean;
  workspace: Workspace;
  member: WorkspaceMember;
}

export interface InvitationInfo {
  workspace_name: string;
  role: string;
  expires_at: string | null;
  email_restricted: boolean;
}

/**
 * Create workspace invitation
 */
export async function createInvitation(
  workspaceId: string,
  data: CreateInvitationRequest
): Promise<WorkspaceInvitation> {
  return apiClient.post<WorkspaceInvitation>(
    `/api/v1/workspaces/${workspaceId}/invitations`,
    data
  );
}

/**
 * List workspace invitations
 */
export async function listInvitations(
  workspaceId: string,
  includeAccepted: boolean = false
): Promise<WorkspaceInvitation[]> {
  const params = new URLSearchParams();
  if (includeAccepted) {
    params.append('include_accepted', 'true');
  }

  const url = `/api/v1/workspaces/${workspaceId}/invitations${
    params.toString() ? `?${params.toString()}` : ''
  }`;

  return apiClient.get<WorkspaceInvitation[]>(url);
}

/**
 * Delete (revoke) invitation
 */
export async function deleteInvitation(
  workspaceId: string,
  invitationId: number
): Promise<void> {
  return apiClient.delete<void>(
    `/api/v1/workspaces/${workspaceId}/invitations/${invitationId}`
  );
}

/**
 * Accept invitation
 */
export async function acceptInvitation(
  token: string
): Promise<AcceptInvitationResponse> {
  return apiClient.post<AcceptInvitationResponse>(
    '/api/v1/invitations/accept',
    { token }
  );
}

/**
 * Get invitation info (public endpoint, no auth required)
 */
export async function getInvitationInfo(
  token: string
): Promise<InvitationInfo> {
  return apiClient.get<InvitationInfo>(`/api/v1/invitations/${token}`);
}

/**
 * Pending invitation for notification bell
 * Issue #179: In-app invitation notifications
 */
export interface PendingInvitation {
  id: number;
  workspace_id: string;
  workspace_name: string;
  role: 'owner' | 'admin' | 'member' | 'viewer';
  invited_by: string;
  expires_at: string | null;
  created_at: string;
  token: string;
  invitation_url: string;
}

export interface PendingInvitationsResponse {
  pending_invitations: PendingInvitation[];
  count: number;
}

/**
 * Get pending invitations for current user
 * Issue #179: Shows in notification bell
 */
export async function getPendingInvitations(): Promise<PendingInvitationsResponse> {
  return apiClient.get<PendingInvitationsResponse>('/api/v1/invitations/pending');
}

/**
 * Member quota status
 * Issue #229: Team member limit (10 members max for Pro plan)
 */
export interface MemberQuota {
  current_members: number;
  pending_invitations: number;
  total_used: number;
  limit: number;
  available: number;
  percentage: number;
  can_invite: boolean;
}

/**
 * Get member quota status for workspace
 * Issue #229: Display seat usage in UI
 */
export async function getMemberQuota(workspaceId: string): Promise<MemberQuota> {
  return apiClient.get<MemberQuota>(`/api/v1/workspaces/${workspaceId}/member-quota`);
}
