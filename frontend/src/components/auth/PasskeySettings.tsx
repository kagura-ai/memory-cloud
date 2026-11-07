"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { startAddAccount } from "@/hooks/useAccountSwitcher";
import { useAuth } from "@/contexts/AuthContext";
import { hasPasswordSignIn, requestPasswordSetup } from "@/lib/auth/auth";
import { apiClient, ApiError } from "@/lib/api/base";
import {
  managementProof,
  passkeyLimit,
  passkeyPath,
  registerPasskey,
  type Passkey,
} from "@/lib/auth/passkeys";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ErrorBanner } from "@/components/common/ErrorBanner";
import { InlineSpinner } from "@/components/common/LoadingState";
import { useToast } from "@/hooks/use-toast";

type Operation = "add" | "remove" | "rename";

export default function PasskeySettings({
  onAvailabilityChange,
}: {
  onAvailabilityChange: (hasPasskey: boolean) => void;
}) {
  const t = useTranslations("passkeys");
  const tPassword = useTranslations("passwordSettings");
  const tCommon = useTranslations("common");
  const { user, refetchUser } = useAuth();
  const { toast } = useToast();
  const [supported, setSupported] = useState(false);
  const [keys, setKeys] = useState<Passkey[]>([]);
  const [ready, setReady] = useState(false);
  const [password, setPassword] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [setupEmail, setSetupEmail] = useState<"idle" | "sending" | "sent">(
    "idle",
  );
  useEffect(() => {
    if (typeof PublicKeyCredential === "undefined" || !user?.id) return;
    setSupported(true);
    let current = true;
    setReady(false);
    setLoadFailed(false);
    setSetupEmail("idle");
    setPassword("");
    setKeys([]);
    apiClient
      .get<Passkey[]>(passkeyPath)
      .then((rows) => {
        if (current) {
          setKeys(rows);
          setReady(true);
        }
      })
      .catch(() => {
        if (current) setLoadFailed(true);
      });
    return () => {
      current = false;
    };
  }, [user?.id]);
  useEffect(() => {
    onAvailabilityChange(ready && keys.length > 0);
  }, [keys.length, ready, onAvailabilityChange]);
  const hasPassword = hasPasswordSignIn(user);
  const canSetUpByEmail = Boolean(
    user?.email && !user.email.toLowerCase().endsWith("@local"),
  );
  const needsRemovalMethod = !hasPassword && keys.length === 1;
  if (!supported) return null;
  function showError(description: string) {
    toast({ title: tCommon("error"), description, variant: "destructive" });
  }
  // #2699: the card shows its own copy, never the server's prose (English, for
  // API clients) or the browser's. What a 403 or 409 means depends on what was
  // being done and with which proof, which only this card knows.
  function errorText(e: unknown, operation: Operation): string {
    if (!(e instanceof ApiError)) {
      // WebAuthn rejects with a DOMException: a closed or timed-out prompt is
      // NotAllowedError; a device that already holds a key for this account
      // refuses to create another with InvalidStateError. Both checks: jsdom's
      // DOMException is not an Error.
      const name =
        e instanceof DOMException || e instanceof Error ? e.name : "";
      if (name === "NotAllowedError") return t("errors.cancelled");
      if (name === "InvalidStateError") return t("errors.alreadyRegistered");
      return t("error");
    }
    if (e.status === 429) return t("errors.tooManyAttempts");
    if (e.status === 404) return t("errors.changed");
    if (operation === "rename") return t("error");
    // An assertion that did not verify, an expired challenge, or a session
    // that has ended.
    if (e.status === 401) return t("errors.verifyFailed");
    if (e.status === 403) {
      if (password) return t("errors.wrongPassword");
      // Removal never accepts a recent sign-in; adding does.
      if (operation === "remove") return t("errors.removeProof");
      return t(hasPassword ? "errors.passwordRequired" : "errors.signInAgain");
    }
    if (e.status === 409) {
      // The other 409s (no other passkey, already registered, only sign-in
      // method) mean the list on screen is out of date.
      return operation === "add" && keys.length >= passkeyLimit
        ? t("errors.limitReached")
        : t("errors.changed");
    }
    return t("error");
  }
  async function change(operation: Operation, run: () => Promise<void>) {
    setBusy(true);
    try {
      await run();
      setEditing(null);
    } catch (e) {
      showError(errorText(e, operation));
    } finally {
      setPassword("");
      setBusy(false);
    }
  }
  // The same request as the Password section's button, sent from here so the
  // link does something visible (#2699).
  async function sendSetupEmail() {
    setSetupEmail("sending");
    try {
      await requestPasswordSetup();
      setSetupEmail("sent");
    } catch (e) {
      setSetupEmail("idle");
      const status = e instanceof ApiError ? e.status : 0;
      if (status === 409) {
        // A password was set elsewhere: refresh, and the card offers it. A
        // refresh that failed resolves without a user, so say so.
        if (!(await refetchUser())) showError(t("errors.changed"));
      } else {
        showError(
          status === 429
            ? tPassword("tooManyRequests")
            : tPassword("setupError"),
        );
      }
    }
  }
  return (
    <Card id="profile-passkeys">
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <ErrorBanner error={loadFailed ? t("errors.load") : null} />

        {keys.map((key) => (
          <div key={key.id} className="space-y-2 border-b pb-3">
            <p className="font-medium">{key.name}</p>
            <p className="text-sm text-muted-foreground">
              {t("created", {
                date: new Date(key.created_at + "Z").toLocaleDateString(),
              })}{" "}
              ·{" "}
              {key.last_used_at
                ? t("used", {
                    date: new Date(key.last_used_at + "Z").toLocaleDateString(),
                  })
                : t("unused")}
            </p>
            {editing === key.id ? (
              <form
                className="flex gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  void change("rename", async () => {
                    const updated = await apiClient.patch<Passkey>(
                      passkeyPath + "/" + key.id,
                      { name },
                    );
                    setKeys((items) =>
                      items.map((item) =>
                        item.id === updated.id ? updated : item,
                      ),
                    );
                  });
                }}
              >
                <Input
                  aria-label={t("name")}
                  value={name}
                  maxLength={100}
                  onChange={(e) => setName(e.target.value)}
                />
                <Button disabled={busy || !name.trim()}>{t("save")}</Button>
                <Button
                  variant="outline"
                  type="button"
                  onClick={() => setEditing(null)}
                >
                  {t("cancel")}
                </Button>
              </form>
            ) : (
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    setEditing(key.id);
                    setName(key.name);
                  }}
                >
                  {t("rename")}
                </Button>
                <Button
                  variant="outline"
                  disabled={busy || needsRemovalMethod}
                  aria-describedby={
                    needsRemovalMethod ? "passkey-removal-help" : undefined
                  }
                  onClick={() =>
                    void change("remove", async () => {
                      const proof = await managementProof(
                        password,
                        keys.length > 1,
                        key.id,
                      );
                      await apiClient.delete(passkeyPath + "/" + key.id, {
                        body: JSON.stringify(proof),
                      });
                      setKeys((items) =>
                        items.filter((item) => item.id !== key.id),
                      );
                    })
                  }
                >
                  {t("remove")}
                </Button>
              </div>
            )}
          </div>
        ))}
        {needsRemovalMethod ? (
          <p id="passkey-removal-help" className="text-sm">
            {t(canSetUpByEmail ? "onlyKeyRemoval" : "onlyKeyAddAnother")}
          </p>
        ) : (
          <p className="text-sm">{t("proof")}</p>
        )}
        {!hasPassword && keys.length <= 1 && (
          <div className="flex flex-wrap items-center gap-3">
            {keys.length === 0 && user?.auth_provider === "google" && (
              <Button
                variant="outline"
                onClick={() =>
                  startAddAccount("google", {
                    returnTo: "/profile",
                    linkProof: true,
                  })
                }
              >
                {t("confirmGoogle")}
              </Button>
            )}
            {canSetUpByEmail &&
              (setupEmail === "sent" ? (
                <p role="status" className="text-sm">
                  {tPassword("setupSent", { email: user?.email ?? "" })}
                </p>
              ) : (
                <Button
                  variant="link"
                  className="h-auto p-0 underline"
                  disabled={setupEmail === "sending"}
                  onClick={() => void sendSetupEmail()}
                >
                  {setupEmail === "sending" && <InlineSpinner />}
                  {setupEmail === "sending"
                    ? tPassword("setupSending")
                    : t("setupPassword")}
                </Button>
              ))}
          </div>
        )}
        {hasPassword && (
          <div>
            <Label htmlFor="passkey-password">{t("password")}</Label>
            <Input
              id="passkey-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
        )}
        <Button
          disabled={!ready || busy}
          onClick={() =>
            void change("add", async () => {
              const added = await registerPasskey(
                await managementProof(password, keys.length > 0),
              );
              setKeys((items) => [...items, added]);
            })
          }
        >
          {t("add")}
        </Button>
      </CardContent>
    </Card>
  );
}
