/**
 * Workspace allowance of the signed-in owner (#2919, account API of #2918).
 *
 *   GET  /api/v1/me/workspace-slots                          status
 *   POST /api/v1/me/workspace-slots/preview                  what a selection would do; saves nothing
 *   PUT  /api/v1/me/workspace-slots/selection                save (and apply) the selection
 *        409 SLOT-003 stale `expected_version`, 409 SLOT-002 no slot can be
 *        freed for a suspended workspace, 422 VAL-001 an id the owner does not own
 *   GET  /api/v1/me/workspace-slots/workspaces/{id}/contexts the contexts the owner may export
 *
 * Session only and experimental. Every route answers 404 while the deployment
 * has the feature off (`features.workspace_slots` false).
 */

import { apiClient } from "./base";

export type WorkspaceSlotStatus = "active" | "slot_suspended";

/** Which allowance a preview runs against. */
export type WorkspaceSlotScenario = "current" | "paid_plans_end";

export interface WorkspaceSlotAllowance {
  /** Owned workspaces this account may keep active. */
  cap: number;
  base: number;
  slot_bonus: number;
  tier_grant: number;
  /** The highest plan among the owned workspaces. */
  tier: string;
  /** The next plan that grants more workspaces; null on the top plan. */
  next_tier: string | null;
  /** The allowance on `next_tier`; null when there is none. */
  cap_on_next_tier: number | null;
}

/** An integration service in force for the workspace now (#3010). */
export interface SlotService {
  /** Display name (the deployment's mapping), else the service id. */
  name: string;
  /** When access ends (UTC, ISO 8601). */
  valid_until: string;
}

export interface SlotWorkspace {
  id: string;
  name: string;
  plan_name: string;
  status: WorkspaceSlotStatus;
  suspended_at: string | null;
  /** A paid subscription runs (or runs until the end of its term). */
  under_billing_contract: boolean;
  /** The owner's own current workspace. */
  is_current: boolean;
  created_at: string;
  /** Services in force now, sorted by name; empty when none (#3010). */
  services?: SlotService[];
}

/** The outcome if every paid subscription ended now (id arrays). */
export interface PaidPlansEndOutcome {
  cap: number;
  active: string[];
  suspended: string[];
}

export interface WorkspaceSlots {
  allowance: WorkspaceSlotAllowance;
  owned_count: number;
  active_count: number;
  /** The number of workspaces that may be active now ("Active X of N"). */
  active_slots: number;
  contracted_count: number;
  selection_required: boolean;
  selection: { workspace_ids: string[]; saved_at: string | null };
  /** Send back as `expected_version` when saving. */
  version: number;
  /** null when no owned workspace is under a billing contract. */
  if_paid_plans_end: PaidPlansEndOutcome | null;
  workspaces: SlotWorkspace[];
}

export interface WorkspaceSlotPreview {
  scenario: WorkspaceSlotScenario;
  cap: number;
  active_slots: number;
  active: string[];
  suspended: string[];
  /** Selected ids that would stay suspended. */
  unmet: string[];
  /** Relative to the active set now. */
  changes: { suspend: string[]; resume: string[] };
}

export interface SlotWorkspaceContext {
  id: string;
  name: string;
  /** The name to show when set (design 12.7); `name` otherwise. */
  display_name: string | null;
  is_private: boolean;
}

export interface SlotWorkspaceContexts {
  workspace_id: string;
  contexts: SlotWorkspaceContext[];
}

/** The `details` of a 409 `SLOT-002`. */
export interface WorkspaceSlotsFullDetails {
  cap?: number;
  active_slots?: number;
  active_count?: number;
  contracted_count?: number;
  next_tier?: string | null;
  cap_on_next_tier?: number | null;
  unmet?: string[];
}

const BASE = "/api/v1/me/workspace-slots";

export function getWorkspaceSlots(): Promise<WorkspaceSlots> {
  return apiClient.get<WorkspaceSlots>(BASE);
}

export function previewWorkspaceSlots(
  workspaceIds: string[],
  scenario: WorkspaceSlotScenario,
): Promise<WorkspaceSlotPreview> {
  return apiClient.post<WorkspaceSlotPreview>(`${BASE}/preview`, {
    workspace_ids: workspaceIds,
    scenario,
  });
}

export function saveWorkspaceSelection(
  workspaceIds: string[],
  expectedVersion: number,
): Promise<WorkspaceSlots> {
  return apiClient.put<WorkspaceSlots>(`${BASE}/selection`, {
    workspace_ids: workspaceIds,
    expected_version: expectedVersion,
  });
}

export function listSlotWorkspaceContexts(
  workspaceId: string,
): Promise<SlotWorkspaceContexts> {
  return apiClient.get<SlotWorkspaceContexts>(
    `${BASE}/workspaces/${encodeURIComponent(workspaceId)}/contexts`,
  );
}
