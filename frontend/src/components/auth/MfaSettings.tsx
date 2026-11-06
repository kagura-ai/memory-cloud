"use client";

import { useState, type FormEvent } from "react";
import { QRCodeSVG } from "qrcode.react";
import { useTranslations } from "next-intl";
import { useAuth } from "@/contexts/AuthContext";
import { apiClient } from "@/lib/api/base";
import { hasPasswordSignIn } from "@/lib/auth/auth";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Step = "setup" | "verify" | "disable" | "regenerate" | "codes";
interface Enrollment { secret: string; provisioning_uri: string }

export function MfaSettings() {
  const { user, refetchUser } = useAuth();
  const t = useTranslations("mfaSettings");
  const [step, setStep] = useState<Step | null>(null);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [codes, setCodes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  if (!hasPasswordSignIn(user)) return null;

  function close() {
    setStep(null);
    setEnrollment(null);
    setPassword("");
    setCode("");
    setCodes([]);
    setError("");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (step === "setup") {
        const data = await apiClient.post<Enrollment>("/api/v1/auth/mfa/setup", { current_password: password });
        setPassword("");
        setEnrollment(data);
        setStep("verify");
      } else if (step === "verify" || step === "regenerate") {
        const data = step === "verify"
          ? await apiClient.post<{recovery_codes: string[]}>("/api/v1/auth/mfa/enable", { code })
          : await apiClient.post<{recovery_codes: string[]}>("/api/v1/auth/mfa/recovery-codes/regenerate", { current_password: password, code });
        setPassword("");
        setCode("");
        setEnrollment(null);
        setCodes(data.recovery_codes);
        setStep("codes");
        await refetchUser();
      } else if (step === "disable") {
        await apiClient.post("/api/v1/auth/mfa/disable", { current_password: password, code });
        close();
        await refetchUser();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("error"));
    } finally {
      setBusy(false);
    }
  }

  return <Card>
    <CardHeader><CardTitle>{t("title")}</CardTitle></CardHeader>
    <CardContent className="space-y-3">
      <p>{user?.mfa_enabled ? t("enabled") : t("disabled")}</p>
      {user?.mfa_enabled && <div className="space-y-2">
        {typeof user.mfa_recovery_codes_remaining === "number" &&
          <p>{t("remaining", { count: user.mfa_recovery_codes_remaining })}</p>}
        <Button variant="outline" onClick={() => setStep("regenerate")}>{t("regenerate")}</Button>
      </div>}
      <Button variant="outline" onClick={() => setStep(user?.mfa_enabled ? "disable" : "setup")}>
        {user?.mfa_enabled ? t("disable") : t("setup")}
      </Button>
      <Dialog open={step !== null} onOpenChange={open => { if (!open && !busy) close(); }}>
        <DialogContent>
          <DialogTitle>{step === "codes" ? t("codesTitle") : t("title")}</DialogTitle>
          <DialogDescription>{step ? t(step + "Hint") : ""}</DialogDescription>
          {step === "codes" ? <>
            <ul className="grid gap-1 font-mono text-sm">{codes.map(value => <li key={value}>{value}</li>)}</ul>
            <Button onClick={close}>{t("saved")}</Button>
          </> : <form onSubmit={submit} className="space-y-4">
            {(step === "setup" || step === "disable" || step === "regenerate") && <div>
              <Label htmlFor="mfa-password">{t("password")}</Label>
              <Input id="mfa-password" type="password" autoComplete="current-password" required
                value={password} onChange={e => setPassword(e.target.value)} />
            </div>}
            {step === "verify" && enrollment && <div className="space-y-2">
              <QRCodeSVG value={enrollment.provisioning_uri} size={160} marginSize={4} title={t("qr")} />
              <p>{t("manual")}</p><code className="break-all select-all">{enrollment.secret}</code>
            </div>}
            {(step === "verify" || step === "disable" || step === "regenerate") && <div>
              <Label htmlFor="mfa-code">{step === "disable" ? t("codeOrRecovery") : t("code")}</Label>
              <Input id="mfa-code" autoComplete="one-time-code" required maxLength={step === "disable" ? 35 : 6}
                value={code} onChange={e => setCode(e.target.value.trim())} />
            </div>}
            {error && <p role="alert" className="text-destructive">{error}</p>}
            <Button type="submit" disabled={busy}>{t(step === "verify" ? "enable" : step ?? "setup")}</Button>
          </form>}
        </DialogContent>
      </Dialog>
    </CardContent>
  </Card>;
}
