"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { signInWithPasskey } from "@/lib/auth/passkeys";

export default function PasskeyLogin({ enabled, conditional, returnTo, terms }: {
  enabled: boolean; conditional: boolean; returnTo: string; terms?: string;
}) {
  const t = useTranslations("passkeys");
  const [supported, setSupported] = useState(false);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => { setSupported(typeof PublicKeyCredential !== "undefined"); }, []);
  useEffect(() => {
    if (!enabled || !supported || !conditional) return;
    const controller = new AbortController();
    active.current = controller;
    void (async () => {
      if (!await PublicKeyCredential.isConditionalMediationAvailable?.() || controller.signal.aborted) return;
      const target = await signInWithPasskey(returnTo, terms, controller.signal, true);
      if (!controller.signal.aborted) window.location.assign(target);
    })().catch(() => {}); // Autofill is optional; the explicit button remains available.
    return () => controller.abort();
  }, [enabled, supported, conditional, returnTo, terms]);
  useEffect(() => () => active.current?.abort(), []);
  if (!supported) return null;
  async function signIn() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError(false);
    try {
      const target = await signInWithPasskey(returnTo, terms, controller.signal);
      if (!controller.signal.aborted) window.location.assign(target);
    } catch {
      if (!controller.signal.aborted) setError(true);
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return <>
    <Button type="button" variant="outline" className="w-full" disabled={!enabled || busy} onClick={() => void signIn()}>{t("signIn")}</Button>
    {error && <p role="alert" className="text-sm text-destructive">{t("signInError")}</p>}
  </>;
}
