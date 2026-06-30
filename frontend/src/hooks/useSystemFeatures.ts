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

let cache: Features | null = null;
let inflight: Promise<Features> | null = null;

export function useSystemFeatures(): Features | null {
  const [features, setFeatures] = useState<Features | null>(cache);

  useEffect(() => {
    if (cache) {
      setFeatures(cache);
      return;
    }
    if (!inflight) {
      inflight = getSystemInfo()
        .then((info) => {
          cache = info.features ?? {};
          return cache;
        })
        .catch((e) => {
          // Fetch failure → treat as no features (everything default-off, fail
          // closed). Surface it in dev so a /system/info outage isn't silently
          // invisible. Don't cache, so a later component mount can retry.
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
