"use client";

/**
 * Device Authorization Page (RFC 8628 — Issue #536)
 *
 * User enters an 8-character user_code displayed in their CLI, then approves
 * or denies the device authorization request on the consent screen.
 */

import { useEffect, useRef, useState, Suspense } from "react";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { ApiError } from "@/lib/api";
import {
  verifyDeviceCode,
  confirmDevice,
  type DeviceVerifyResponse,
} from "@/lib/auth/auth";
import { AlertCircle, CheckCircle2, XCircle, Monitor } from "lucide-react";

type Phase =
  | "input"
  | "verifying"
  | "consent"
  | "submitting"
  | "success"
  | "denied"
  | "error";

const SCOPE_LABEL_MAP: Record<string, string> = {
  "memory:read": "device.scopeRead",
  "memory:write": "device.scopeWrite",
  "memory:delete": "device.scopeDelete",
  "memory:admin": "device.scopeAdmin",
  offline_access: "device.scopeOffline",
};

function DevicePageInner() {
  const t = useTranslations();
  const searchParams = useSearchParams();

  const [phase, setPhase] = useState<Phase>("input");
  const [userCode, setUserCode] = useState(searchParams.get("user_code") ?? "");
  const [deviceInfo, setDeviceInfo] = useState<DeviceVerifyResponse | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const submittingRef = useRef(false);

  useEffect(() => {
    const codeFromUrl = searchParams.get("user_code");
    if (codeFromUrl && codeFromUrl.length === 8) {
      setUserCode(codeFromUrl);
      verifyCode(codeFromUrl);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  async function verifyCode(code: string) {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setPhase("verifying");
    setError(null);

    try {
      const info = await verifyDeviceCode(code);
      if (info.is_authorized) {
        setPhase("success");
      } else if (info.is_expired) {
        setError(t("device.expired"));
        setPhase("error");
      } else {
        setDeviceInfo(info);
        setPhase("consent");
      }
    } catch {
      setError(t("device.invalidCode"));
      setPhase("error");
    } finally {
      submittingRef.current = false;
    }
  }

  async function handleConfirm(approve: boolean) {
    if (submittingRef.current || !deviceInfo) return;
    submittingRef.current = true;
    setPhase("submitting");
    setError(null);

    try {
      const result = await confirmDevice(deviceInfo.user_code, approve);
      setPhase(result.status === "approved" ? "success" : "denied");
    } catch (err) {
      const detail =
        err instanceof ApiError ? err.message : t("device.errorMessage");
      if (detail.includes("already been authorized")) {
        setPhase("success");
      } else if (detail.includes("already been denied")) {
        setPhase("denied");
      } else if (detail.includes("already been processed")) {
        // Double-submit race: re-verify to determine actual state
        try {
          const info = await verifyDeviceCode(deviceInfo.user_code);
          setPhase(info.is_authorized ? "success" : "denied");
        } catch {
          setError(detail);
          setPhase("error");
        }
      } else {
        setError(detail);
        setPhase("error");
      }
    } finally {
      submittingRef.current = false;
    }
  }

  function handleCodeChange(value: string) {
    const cleaned = value.replace(/[^a-zA-Z0-9]/g, "").toUpperCase();
    setUserCode(cleaned);
    setError(null);
    if (phase === "error") setPhase("input");
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && userCode.length === 8 && phase === "input") {
      e.preventDefault();
      verifyCode(userCode);
    }
  }

  function resetToInput() {
    setPhase("input");
    setUserCode("");
    setDeviceInfo(null);
    setError(null);
  }

  function renderScopeBadges(scopes: string | null) {
    if (!scopes) return null;
    return (
      <div className="flex flex-wrap gap-2 justify-center">
        {scopes
          .split(" ")
          .filter(Boolean)
          .map((scope) => {
            const labelKey = SCOPE_LABEL_MAP[scope];
            return (
              <Badge key={scope} variant="secondary">
                {labelKey ? t(labelKey) : t("device.unknownScope", { scope })}
              </Badge>
            );
          })}
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 px-4">
      <Card className="w-full max-w-md">
        <CardContent className="pt-8 pb-8 space-y-6">
          {phase === "success" && (
            <div className="text-center space-y-4">
              <CheckCircle2 className="mx-auto h-12 w-12 text-green-500" />
              <h1 className="text-xl font-semibold text-gray-900">
                {t("device.successTitle")}
              </h1>
              <p className="text-sm text-gray-600">
                {t("device.successMessage")}
              </p>
            </div>
          )}

          {phase === "denied" && (
            <div className="text-center space-y-4">
              <XCircle className="mx-auto h-12 w-12 text-red-500" />
              <h1 className="text-xl font-semibold text-gray-900">
                {t("device.deniedTitle")}
              </h1>
              <p className="text-sm text-gray-600">
                {t("device.deniedMessage")}
              </p>
              <Button variant="outline" onClick={resetToInput} className="mt-4">
                {t("device.backToVerify")}
              </Button>
            </div>
          )}

          {(phase === "input" ||
            phase === "verifying" ||
            phase === "error") && (
            <div className="text-center space-y-4">
              <Monitor className="mx-auto h-10 w-10 text-gray-400" />
              <div>
                <h1 className="text-xl font-semibold text-gray-900">
                  {t("device.title")}
                </h1>
                <p className="mt-1 text-sm text-gray-600">
                  {t("device.description")}
                </p>
              </div>

              <div className="space-y-2">
                <Label htmlFor="userCode" className="text-gray-700">
                  {t("device.codeLabel")}
                </Label>
                <Input
                  id="userCode"
                  type="text"
                  maxLength={8}
                  value={userCode}
                  onChange={(e) => handleCodeChange(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={t("device.codePlaceholder")}
                  className="bg-white text-gray-900 text-center text-2xl tracking-[0.3em]"
                  autoFocus
                  autoComplete="off"
                  disabled={phase === "verifying"}
                />
              </div>

              {phase === "verifying" && (
                <p className="text-sm text-gray-500">{t("device.verifying")}</p>
              )}

              {phase === "error" && error && (
                <Alert variant="destructive">
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}

              {phase === "error" && (
                <Button variant="outline" onClick={resetToInput}>
                  {t("device.backToVerify")}
                </Button>
              )}
            </div>
          )}

          {(phase === "consent" || phase === "submitting") && deviceInfo && (
            <div className="text-center space-y-4">
              <div>
                <h1 className="text-xl font-semibold text-gray-900">
                  {t("device.consentTitle")}
                </h1>
                <p className="mt-2 text-sm text-gray-600">
                  {t("device.consentDescription")}
                </p>
              </div>

              <p className="text-lg font-medium text-gray-900">
                {deviceInfo.client_name}
              </p>

              {renderScopeBadges(deviceInfo.scope)}

              <div className="flex gap-3 justify-center pt-2">
                <Button
                  variant="outline"
                  onClick={() => handleConfirm(false)}
                  disabled={phase === "submitting"}
                  className="min-w-[120px]"
                >
                  {phase === "submitting"
                    ? t("device.denying")
                    : t("device.deny")}
                </Button>
                <Button
                  onClick={() => handleConfirm(true)}
                  disabled={phase === "submitting"}
                  className="min-w-[120px] bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white shadow-lg hover:from-brand-green-700 hover:to-emerald-700 disabled:opacity-50"
                >
                  {phase === "submitting"
                    ? t("device.approving")
                    : t("device.approve")}
                </Button>
              </div>
            </div>
          )}

          {phase === "error" && error && deviceInfo && (
            <div className="text-center space-y-4">
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
              <div className="flex gap-3 justify-center">
                <Button variant="outline" onClick={resetToInput}>
                  {t("device.backToVerify")}
                </Button>
                <Button
                  variant="default"
                  onClick={() => {
                    setPhase("consent");
                    setError(null);
                  }}
                >
                  {t("device.tryAgain")}
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export default function DevicePage() {
  return (
    <Suspense fallback={<SpinnerLoading size="lg" />}>
      <DevicePageInner />
    </Suspense>
  );
}
