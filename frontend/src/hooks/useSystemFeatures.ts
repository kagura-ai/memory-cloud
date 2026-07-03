"use client";

/**
 * useSystemFeatures (#1145)
 *
 * Reads the backend feature flags from `GET /api/v1/system/info` and exposes
 * them to UI surfaces that gate on a deployment toggle (e.g. the Plan page).
 *
 * Module-cached so multiple consumers (sidebar + plan page) share a single
 * fetch per session. Returns `null` while the first fetch is in flight; callers
 * should treat a missing flag as **disabled** (default-off semantics).
 */

import { useEffect, useState } from "react";
import { getSystemInfo } from "@/lib/api/system";

type Features = Record<string, boolean>;

// Retry a transient /system/info blip before falling back. A single failed
// fetch used to resolve to {} (everything default-OFF), which is wrong for
// DEFAULT-ON flags like `byok`: a momentary outage would hide the console /
// render a definitive "not enabled" notice for a feature that is actually on
// (v0.42 review #7/#12). Retrying keeps the hook in the `null` (loading) state
// so callers show a loader, not a terminal disabled state, until the flag is
// truly known.
const MAX_ATTEMPTS = 3;
const RETRY_BASE_MS = 500;

let cache: Features | null = null;
let inflight: Promise<Features> | null = null;

async function fetchFeaturesWithRetry(): Promise<Features> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      const info = await getSystemInfo();
      return info.features ?? {};
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

export function useSystemFeatures(): Features | null {
  const [features, setFeatures] = useState<Features | null>(cache);

  useEffect(() => {
    if (cache) {
      setFeatures(cache);
      return;
    }
    if (!inflight) {
      inflight = fetchFeaturesWithRetry()
        .then((f) => {
          cache = f;
          return cache;
        })
        .catch((e) => {
          // Persistent failure after retries → fail closed (default-off).
          // Surface it in dev so a real /system/info outage isn't silently
          // invisible. Don't cache, so a later component mount retries.
          if (process.env.NODE_ENV === "development") {
            // eslint-disable-next-line no-console
            console.error("useSystemFeatures: /system/info fetch failed", e);
          }
          inflight = null;
          return {} as Features;
        });
    }
    let alive = true;
    inflight.then((f) => {
      if (alive) setFeatures(f);
    });
    return () => {
      alive = false;
    };
  }, []);

  return features;
}
