"use client";

/**
 * useSystemFeatures (#1145) / useSystemInfo (#1572)
 *
 * Reads `GET /api/v1/system/info` and exposes it to UI surfaces that gate on a
 * deployment toggle (e.g. the Plan page) or render a deployment default (the
 * reranker card).
 *
 * Module-cached so multiple consumers (sidebar + plan page + settings) share a
 * single fetch per session. Both hooks return `null` while the first fetch is
 * in flight; callers should treat a missing flag as **disabled** (default-off
 * semantics) and a missing `search_defaults` as unknown.
 */

import { useEffect, useState } from "react";
import {
  getSystemInfo,
  type SystemFeatures,
  type SystemInfo,
} from "@/lib/api/system";

type Features = SystemFeatures;

// Retry a transient /system/info blip before falling back. A single failed
// fetch used to resolve to {} (everything default-OFF), which is wrong for
// DEFAULT-ON flags like `byok`: a momentary outage would hide the console /
// render a definitive "not enabled" notice for a feature that is actually on
// (v0.42 review #7/#12). Retrying keeps the hook in the `null` (loading) state
// so callers show a loader, not a terminal disabled state, until the flag is
// truly known.
const MAX_ATTEMPTS = 3;
const RETRY_BASE_MS = 500;

// Persistent failure → fail closed: every gated feature reads as disabled and
// no deployment default is known. This is the OPPOSITE direction to the tier
// matrix (`usePlanFeatures` stays pending on failure), on purpose; the
// `resolveGate` docblock in `lib/gates/featureGates.ts` documents both
// directions, and how a gate composes them, in one place.
const FAILED_INFO: SystemInfo = {
  name: "",
  version: "",
  description: "",
  environment: "",
  features: {},
};

let cache: SystemInfo | null = null;
let inflight: Promise<SystemInfo> | null = null;

async function fetchInfoWithRetry(): Promise<SystemInfo> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      return await getSystemInfo();
    } catch (e) {
      lastError = e;
      if (attempt < MAX_ATTEMPTS) {
        await new Promise((resolve) =>
          setTimeout(resolve, RETRY_BASE_MS * attempt),
        );
      }
    }
  }
  throw lastError;
}

function useCachedSystemInfo(): SystemInfo | null {
  const [info, setInfo] = useState<SystemInfo | null>(cache);

  useEffect(() => {
    if (cache) {
      setInfo(cache);
      return;
    }
    if (!inflight) {
      inflight = fetchInfoWithRetry()
        .then((i) => {
          cache = i;
          return cache;
        })
        .catch((e) => {
          // Surface it in dev so a real /system/info outage isn't silently
          // invisible. Don't cache, so a later component mount retries.
          if (process.env.NODE_ENV === "development") {
            // eslint-disable-next-line no-console
            console.error("useSystemFeatures: /system/info fetch failed", e);
          }
          inflight = null;
          return FAILED_INFO;
        });
    }
    let alive = true;
    inflight.then((i) => {
      if (alive) setInfo(i);
    });
    return () => {
      alive = false;
    };
  }, []);

  return info;
}

export function useSystemFeatures(): Features | null {
  const info = useCachedSystemInfo();
  return info ? (info.features ?? {}) : null;
}

/**
 * The whole `/system/info` payload (features + `search_defaults`), from the
 * same cache and retry as `useSystemFeatures` (#1572).
 */
export function useSystemInfo(): SystemInfo | null {
  return useCachedSystemInfo();
}
