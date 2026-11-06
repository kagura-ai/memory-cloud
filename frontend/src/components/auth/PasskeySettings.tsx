"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { startAddAccount } from "@/hooks/useAccountSwitcher";
import { useAuth } from "@/contexts/AuthContext";
import { hasPasswordSignIn } from "@/lib/auth/auth";
import { apiClient } from "@/lib/api/base";
import {
  managementProof,
  passkeyPath,
  registerPasskey,
  type Passkey,
} from "@/lib/auth/passkeys";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function PasskeySettings({
  onAvailabilityChange,
}: {
  onAvailabilityChange: (hasPasskey: boolean) => void;
}) {
  const t = useTranslations("passkeys");
  const { user } = useAuth();
  const [supported, setSupported] = useState(false);
  const [keys, setKeys] = useState<Passkey[]>([]);
  const [ready, setReady] = useState(false);
  const [password, setPassword] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (typeof PublicKeyCredential === "undefined" || !user?.id) return;
    setSupported(true);
    let current = true;
    setReady(false);
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
      .catch((e) => {
        if (current) setError(e.message);
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
  async function change(operation: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await operation();
      setEditing(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("error"));
    } finally {
      setPassword("");
      setBusy(false);
    }
  }
  return (
    <Card id="profile-passkeys">
      <CardHeader>
        <CardTitle>{t("title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && <p role="alert">{error}</p>}
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
                  void change(async () => {
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
                    void change(async () => {
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
            {canSetUpByEmail && (
              <a href="#profile-password" className="underline">
                {t("setupPassword")}
              </a>
            )}
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
            void change(async () => {
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
