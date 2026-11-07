"use client";

import { useEffect, useRef, useState } from "react";
import { requestPasswordSetup } from "@/lib/auth/auth";

/** Share one request and its result between the profile's sign-in cards. */
export function usePasswordSetup(accountId?: string) {
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const inFlight = useRef<Promise<void> | null>(null);
  const generation = useRef(0);
  useEffect(() => {
    generation.current += 1;
    inFlight.current = null;
    setState("idle");
    return () => { generation.current += 1; };
  }, [accountId]);

  function request(): Promise<void> {
    if (inFlight.current) return inFlight.current;
    const started = generation.current;
    setState("sending");
    const pending = requestPasswordSetup()
      .then(() => {
        if (started === generation.current) setState("sent");
      })
      .catch((error: unknown) => {
        if (started === generation.current) setState("idle");
        throw error;
      })
      .finally(() => {
        if (started === generation.current) inFlight.current = null;
      });
    inFlight.current = pending;
    return pending;
  }

  return { state, request };
}

export type PasswordSetup = ReturnType<typeof usePasswordSetup>;
