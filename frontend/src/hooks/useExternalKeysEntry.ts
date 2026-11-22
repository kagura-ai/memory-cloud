"use client";

/**
 * useExternalKeysEntry (#2968)
 *
 * Whether the Integrations menus offer the External keys entry.
 *
 * - BYOK on: always, to every role (unchanged).
 * - Flags still loading (`null`, which includes the retries of a momentary
 *   /system/info outage): yes, so the entry does not blink away.
 * - BYOK off (an explicit `false`, or a persistent lookup failure, which
 *   reads every flag as off like every other gated feature): only for the
 *   workspace owner whose workspace still stores keys, so they can delete
 *   them (#1167, #1616). The owner-only list is probed once per workspace
 *   and session; a failed probe keeps the entry hidden.
 *
 * `active` lets a caller that renders no entry on the current route skip
 * the probe.
 */

import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { useWorkspaceObjectPresence } from "@/hooks/useWorkspaceObjectPresence";
import { useWorkspace } from "@/contexts/WorkspaceContext";

export function useExternalKeysEntry(active: boolean = true): boolean {
  const features = useSystemFeatures();
  const { currentWorkspace, currentWorkspaceId } = useWorkspace();
  const byokOff = features !== null && features.byok !== true;
  const owner = currentWorkspace?.current_user_role === "owner";
  const stored = useWorkspaceObjectPresence(
    "externalKeys",
    currentWorkspaceId,
    active && byokOff && owner,
  );
  return !byokOff || (owner && stored === true);
}
