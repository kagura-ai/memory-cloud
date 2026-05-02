"use client";

/**
 * useActiveAnalysisPolling — fixed 3-second polling for an in-flight run (#497).
 *
 * Polls ``getAnalysisRun(contextId, runId)`` while the run's status
 * is ``"running"`` and stops as soon as it transitions to a terminal
 * state (succeeded / failed / cancelled). ``visibilitychange`` pause:
 * when the tab is hidden we skip polls to avoid burning quota / cost
 * on a background tab the user does not see — the next visible tick
 * triggers an immediate refetch.
 *
 * Cadence: 3 seconds (fixed). Reasoning is documented in the
 * #497 design HITL — a single fixed cadence is simpler than backoff
 * for v1 runs that typically complete within tens of seconds, and
 * does not penalize "long" runs that are still on the order of a
 * few minutes.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getAnalysisRun, type AnalysisRunRow } from "@/lib/api/analyses";

const POLL_INTERVAL_MS = 3000;

interface UseActiveAnalysisPollingArgs {
  contextId: string;
  runId: string | null;
  /**
   * Called once when the polled run transitions to a terminal state.
   * Use this to refresh the run history list / re-fetch clusters.
   * Receives the final run row.
   */
  onTerminal?: (finalRun: AnalysisRunRow) => void;
}

interface UseActiveAnalysisPollingResult {
  run: AnalysisRunRow | null;
  loading: boolean;
  error: string | null;
  /** Manual refetch — used by the "Refresh" button. */
  refetch: () => void;
}

/**
 * Returns the latest snapshot of the polled run plus a manual
 * ``refetch`` for user-driven refreshes. ``run`` is null while the
 * first poll is in flight or when ``runId`` is null.
 */
export function useActiveAnalysisPolling(
  args: UseActiveAnalysisPollingArgs,
): UseActiveAnalysisPollingResult {
  const { contextId, runId, onTerminal } = args;

  const [run, setRun] = useState<AnalysisRunRow | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Track whether the onTerminal callback has fired for the current
  // (contextId, runId) pair so we don't fire it twice if the polling
  // cycle catches the same terminal state twice (race between manual
  // refetch and the interval tick).
  const terminalFiredRef = useRef<string | null>(null);

  // Stash the latest onTerminal in a ref so the polling loop's
  // useEffect doesn't re-establish itself every time the parent
  // passes a fresh callback identity.
  const onTerminalRef = useRef(onTerminal);
  useEffect(() => {
    onTerminalRef.current = onTerminal;
  }, [onTerminal]);

  // Track the live interval handle so the post-fetch terminal check
  // can clear it from inside ``fetchOnce``. Using a ref means the
  // handle survives across renders without being a useEffect dep.
  const intervalRef = useRef<number | null>(null);

  const fetchOnce = useCallback(async () => {
    if (!runId) return;
    try {
      setLoading(true);
      setError(null);
      const row = await getAnalysisRun(contextId, runId);
      setRun(row);
      if (
        row.status !== "running" &&
        terminalFiredRef.current !== `${contextId}:${runId}`
      ) {
        terminalFiredRef.current = `${contextId}:${runId}`;
        // Stop the poll loop NOW that the run is terminal — the
        // earlier "interval cleanup happens on runId change" path
        // would have left this interval live for the lifetime of
        // the parent component (potentially hours).
        if (intervalRef.current !== null) {
          window.clearInterval(intervalRef.current);
          intervalRef.current = null;
        }
        onTerminalRef.current?.(row);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load run.");
    } finally {
      setLoading(false);
    }
  }, [contextId, runId]);

  // Reset state on (contextId, runId) change so a stale "succeeded"
  // run does not flash while the new one's first poll is in flight.
  useEffect(() => {
    terminalFiredRef.current = null;
    setRun(null);
    setError(null);
    setLoading(runId !== null);
  }, [contextId, runId]);

  // Poll loop. ``runId === null`` short-circuits — no interval armed.
  useEffect(() => {
    if (!runId) return;

    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      if (typeof document !== "undefined" && document.hidden) return;
      await fetchOnce();
    };

    // Kick off the first poll immediately.
    tick();

    intervalRef.current = window.setInterval(tick, POLL_INTERVAL_MS);

    const onVisibilityChange = () => {
      if (typeof document !== "undefined" && !document.hidden) {
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVisibilityChange);

    return () => {
      cancelled = true;
      if (intervalRef.current !== null) {
        window.clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [runId, fetchOnce]);

  return { run, loading, error, refetch: fetchOnce };
}
