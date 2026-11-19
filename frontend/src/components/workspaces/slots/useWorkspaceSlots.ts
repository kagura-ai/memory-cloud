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
import { isNotFound } from "./slotErrors";

interface WorkspaceSlotsState {
  data: WorkspaceSlots | null;
  /** True until the first answer (or failure). */
  loading: boolean;
  /**
   * The API answered 404: the deployment has the feature off although
   * `/system/info` said otherwise (a deploy in progress, a stale answer).
   */
  unavailable: boolean;
  /**
   * Refetch; `background` keeps the current data on screen. Resolves true
   * when fresh data arrived.
   */
  reload: (options?: { background?: boolean }) => Promise<boolean>;
  /** Use a body the server already returned (the PUT answers with it). */
  replace: (data: WorkspaceSlots) => void;
}

export function useWorkspaceSlots(enabled: boolean): WorkspaceSlotsState {
  const [data, setData] = useState<WorkspaceSlots | null>(null);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);
  const request = useRef(0);

  // Applies one answer; false when it is outdated or a failure.
  const settle = useCallback(
    async (id: number, answer: Promise<WorkspaceSlots>): Promise<boolean> => {
      try {
        const next = await answer;
        if (id !== request.current) return false;
        setData(next);
        setUnavailable(false);
        return true;
      } catch (error) {
        if (id === request.current && isNotFound(error)) setUnavailable(true);
        return false;
      } finally {
        if (id === request.current) setLoading(false);
      }
    },
    [],
  );

  const reload = useCallback(
    async ({ background = false }: { background?: boolean } = {}) => {
      if (!background) setLoading(true);
      const id = ++request.current;
      return settle(id, getWorkspaceSlots());
    },
    [settle],
  );

  const replace = useCallback((next: WorkspaceSlots) => {
    ++request.current;
    setData(next);
    setLoading(false);
  }, []);

  // The first load. State is set only once the answer settles.
  useEffect(() => {
    if (!enabled) return;
    const id = ++request.current;
    void getWorkspaceSlots()
      .then((next) => {
        if (id !== request.current) return;
        setData(next);
      })
      .catch((error: unknown) => {
        if (id === request.current && isNotFound(error)) setUnavailable(true);
      })
      .finally(() => {
        if (id === request.current) setLoading(false);
      });
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const refetch = () => void reload({ background: true });
    const onVisible = () => {
      if (document.visibilityState === "visible") refetch();
    };
    window.addEventListener("focus", refetch);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", refetch);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, reload]);

  return { data, loading, unavailable, reload, replace };
}
