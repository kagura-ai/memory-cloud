/**
 * useWorkspaceContextOptions (#2129)
 *
 * The current workspace's contexts as default-context choices
 * (`{id, label}`, label = display name or name). Re-fetched on a workspace
 * switch and emptied when there is no current workspace, so a choice from
 * the previous workspace is never offered. A failed fetch leaves the list
 * empty: "No default" and Clear still work.
 */

"use client";

import { useEffect, useState } from "react";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getContexts } from "@/lib/api/contexts";
import type { DefaultContextOption } from "@/components/credentials/DefaultContextControl";

export function useWorkspaceContextOptions(): DefaultContextOption[] {
  const { currentWorkspaceId } = useWorkspace();
  const [options, setOptions] = useState<DefaultContextOption[]>([]);

  useEffect(() => {
    setOptions([]);
    if (!currentWorkspaceId) return;
    let cancelled = false;
    getContexts()
      .then((resp) => {
        if (cancelled) return;
        setOptions(
          (resp?.contexts ?? []).map((c) => ({
            id: c.id,
            label: c.display_name || c.name,
          })),
        );
      })
      .catch(() => {
        // Empty list (already reset above).
      });
    return () => {
      cancelled = true;
    };
  }, [currentWorkspaceId]);

  return options;
}

/**
 * The label to show for a default: the option's label when the context is
 * offered (display name), else the server's name — so one context never
 * appears under two names on the page.
 */
export function defaultContextLabel(
  options: DefaultContextOption[],
  id: string | null,
  serverName: string | null,
): string | null {
  if (!id) return null;
  return options.find((o) => o.id === id)?.label ?? serverName;
}
