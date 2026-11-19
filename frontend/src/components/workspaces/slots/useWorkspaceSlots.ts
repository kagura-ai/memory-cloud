"use client";

/**
 * The owner's workspace allowance (#2919), loaded once and refetched when the
 * window regains focus, so a return from the billing page shows workspaces an
 * upgrade resumed.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getWorkspaceSlots,
  type WorkspaceSlots,
} from "@/lib/api/workspaceSlots";

export interface WorkspaceSlotsState {
  data: WorkspaceSlots | null;
  /** True until the first answer (or failure). */
  loading: boolean;
  /** The first load failed. A failed background refetch keeps the last data. */
  failed: boolean;
  /** Refetch; `background` keeps the current data on screen. */
  reload: (options?: { background?: boolean }) => Promise<void>;
  /** Use a body the server already returned (the PUT answers with it). */
  replace: (data: WorkspaceSlots) => void;
}

export function useWorkspaceSlots(enabled: boolean): WorkspaceSlotsState {
  const [data, setData] = useState<WorkspaceSlots | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const request = useRef(0);

  // Sets state only after the request settles, so an effect may call it.
  const fetchSlots = useCallback(async (reportFailure: boolean) => {
    const id = ++request.current;
    try {
      const next = await getWorkspaceSlots();
      if (id !== request.current) return;
      setData(next);
      setFailed(false);
    } catch {
      if (id !== request.current) return;
      if (reportFailure) setFailed(true);
    } finally {
      if (id === request.current) setLoading(false);
    }
  }, []);

  const reload = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (!background) {
        setLoading(true);
        setFailed(false);
      }
      await fetchSlots(!background);
    },
    [fetchSlots],
  );

  const replace = useCallback((next: WorkspaceSlots) => {
    ++request.current;
    setData(next);
    setFailed(false);
    setLoading(false);
  }, []);

  // The first load. State is set only in the callbacks (same rules as
  // `fetchSlots`, written out so no state is set inside the effect body).
  useEffect(() => {
    if (!enabled) return;
    const id = ++request.current;
    getWorkspaceSlots()
      .then((next) => {
        if (id !== request.current) return;
        setData(next);
        setFailed(false);
      })
      .catch(() => {
        if (id === request.current) setFailed(true);
      })
      .finally(() => {
        if (id === request.current) setLoading(false);
      });
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const refetch = () => void fetchSlots(false);
    const onVisible = () => {
      if (document.visibilityState === "visible") refetch();
    };
    window.addEventListener("focus", refetch);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", refetch);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, fetchSlots]);

  return { data, loading, failed, reload, replace };
}
