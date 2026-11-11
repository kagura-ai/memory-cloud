"use client";

import { useEffect, useState } from "react";
import {
  getMemoryHealthBreakdown,
  type MemoryHealthBreakdown,
} from "@/lib/api/memory-health";

/**
 * The caller's memory health breakdown (#2764), fetched once per mount.
 *
 * The breakdown is an extra on the contexts pages, never a precondition:
 * `breakdown` stays null while the request is in flight AND when it fails, so
 * a page shows no badge, no card and no Health tab instead of an error.
 * `settled` tells the two apart for callers that need to.
 */
export function useMemoryHealthBreakdown(): {
  breakdown: MemoryHealthBreakdown | null;
  settled: boolean;
} {
  const [breakdown, setBreakdown] = useState<MemoryHealthBreakdown | null>(null);
  const [settled, setSettled] = useState(false);

  useEffect(() => {
    let active = true;
    getMemoryHealthBreakdown()
      .then((data) => {
        if (active) setBreakdown(data);
      })
      .catch(() => {
        // Non-critical: the page renders without health information.
      })
      .finally(() => {
        if (active) setSettled(true);
      });
    return () => {
      active = false;
    };
  }, []);

  return { breakdown, settled };
}
