"use client";

/**
 * Device Authorization Page (RFC 8628 — Issue #536)
 *
 * User enters an 8-character user_code displayed in their CLI, then approves
 * or denies the device authorization request on the consent screen.
 *
 * Auth guard: unauthenticated users are redirected to /login with a return_to
 * param that preserves the user_code so the code is NOT burned before the user
 * logs in. See issue #772.
 *
 * Visual chrome (Issue #633): brought to parity with the /login (MFA) screen —
 * grid + blur-orb background, centered KaguraLogo, top-right LanguageSelector,
 * glassmorphism card, ping-ring Suspense fallback. The state machine, copy,
 * i18n keys, and double-submit guard are unchanged from #536.
 */

import { useEffect, useRef, useState, Suspense } from "react";
import { useTranslations } from "next-intl";
import { useSearchParams, useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { KaguraLogo } from "@/components/icons/KaguraLogo";
import { LanguageSelector } from "@/components/LanguageSelector";
import { ApiError } from "@/lib/api";
import { apiClient } from "@/lib/api/base";
import {
  verifyDeviceCode,
  confirmDevice,
  type DeviceVerifyResponse,
} from "@/lib/auth/auth";
import { useAuth } from "@/contexts/AuthContext";
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

/** Grid + blur-orb gradient background, shared with /login (Issue #633). */
function PageBackground() {
  return (
    <>
      <div className="absolute inset-0 -z-10 bg-[linear-gradient(to_right,#8080800a_1px,transparent_1px),linear-gradient(to_bottom,#8080800a_1px,transparent_1px)] bg-[size:14px_24px]" />
      <div className="absolute inset-0 -z-10 bg-gradient-to-b from-white via-gray-50/50 to-white" />
      <div className="pointer-events-none absolute -left-1/4 -top-1/4 h-96 w-96 rounded-full bg-[#00664b]/10 blur-3xl" />
      <div className="pointer-events-none absolute -right-1/4 -bottom-1/4 h-96 w-96 rounded-full bg-[#faa916]/10 blur-3xl" />
    </>
  );
}

/** Animated ping-ring spinner, shared with /login's Suspense fallback. */
function BrandSpinner() {
  return (
    <div className="relative">
      <div className="h-16 w-16 animate-spin rounded-full border-4 border-[#e6f0ec] border-t-kagura-accent" />
      <div className="absolute inset-0 h-16 w-16 animate-ping rounded-full border-4 border-kagura-accent opacity-20" />
    </div>
  );
}

/** "Device authorization" context pill shown on the input / consent phases. */
function DeviceBadge({ label }: { label: string }) {
  return (
    <div className="mb-6 flex justify-center">
      <div className="inline-flex items-center gap-2 rounded-full bg-[#e6f0ec] px-4 py-1.5 text-sm font-semibold text-kagura-tokiwa">
        <Monitor className="h-4 w-4" />
        <span>{label}</span>
      </div>
    </div>
  );
}

function DevicePageInner() {
  const t = useTranslations();
  const searchParams = useSearchParams();
  const router = useRouter();
  const { user, isLoading: authLoading } = useAuth();

  const [phase, setPhase] = useState<Phase>("input");
  const [userCode, setUserCode] = useState(searchParams.get("user_code") ?? "");
  const [deviceInfo, setDeviceInfo] = useState<DeviceVerifyResponse | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const submittingRef = useRef(false);

  useEffect(() => {
    if (authLoading) return;

    const codeFromUrl = searchParams.get("user_code");

    // Preserve user_code in return_to so the device code is not burned before
    // the unauthenticated user reaches the consent page.
    if (!user) {
      // Fire-and-forget audit ping BEFORE redirecting away — backend has no
      // visibility into unauth hits otherwise. Prefix-only (4 chars) per
      // RFC 8628 §5.2: full user_code is auth material within the TTL.
      // user_code is uppercase alphanumeric only; normalize before sending
      // so malformed/attacker-supplied URLs don't write garbage to audit logs
      // (the backend Pydantic model also enforces ^[A-Z0-9]*$). Issue #779.
      const userCodePrefix = (codeFromUrl ?? "")
        .toUpperCase()
        .replace(/[^A-Z0-9]/g, "")
        .slice(0, 4);
      apiClient
        .post("/api/v1/oauth/device/audit-unauth", {
          user_code_prefix: userCodePrefix,
        })
        .catch(() => {});

      const returnPath = codeFromUrl
        ? `/device?user_code=${encodeURIComponent(codeFromUrl)}`
        : "/device";
      router.replace(`/login?return_to=${encodeURIComponent(returnPath)}`);
      return;
    }

    if (codeFromUrl && codeFromUrl.length === 8) {
      setUserCode(codeFromUrl);
      verifyCode(codeFromUrl);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoading, user, searchParams, router]);

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

  // Show spinner while auth state is resolving or while redirecting unauth users.
  // This prevents a flash of the empty input form before redirect fires.
  if (authLoading || !user) {
    return (
      <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-white px-4">
        <PageBackground />
        <BrandSpinner />
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-start justify-center overflow-hidden bg-white px-4 pt-[16vh]">
      <PageBackground />

      <div className="absolute top-4 right-4 z-10">
        <LanguageSelector
          className="!bg-white/90 backdrop-blur-sm border border-gray-300 shadow-sm hover:!bg-white !text-gray-700 hover:!text-gray-900"
          showLabel
        />
      </div>

      <div className="relative w-full max-w-md px-4">
        <div className="mb-8 flex justify-center">
          <KaguraLogo className="h-24 w-auto" variant="image" />
        </div>

        <Card className="overflow-hidden border-gray-200 bg-white/80 shadow-2xl backdrop-blur-xl">
          <CardContent className="p-8 space-y-6">
            {phase === "success" && (
              <div className="text-center space-y-4">
                <CheckCircle2 className="mx-auto h-12 w-12 text-kagura-tokiwa" />
                <h1 className="text-2xl font-bold text-gray-900">
                  {t("device.successTitle")}
                </h1>
                <p className="text-gray-600">{t("device.successMessage")}</p>
              </div>
            )}

            {phase === "denied" && (
              <div className="text-center space-y-4">
                <XCircle className="mx-auto h-12 w-12 text-red-500" />
                <h1 className="text-2xl font-bold text-gray-900">
                  {t("device.deniedTitle")}
                </h1>
                <p className="text-gray-600">{t("device.deniedMessage")}</p>
                <Button
                  variant="outline"
                  onClick={resetToInput}
                  className="mt-4"
                >
                  {t("device.backToVerify")}
                </Button>
              </div>
            )}

            {(phase === "input" ||
              phase === "verifying" ||
              phase === "error") && (
              <div className="text-center space-y-4">
                <DeviceBadge label={t("device.badge")} />
                <div>
                  <h1 className="text-3xl font-bold text-gray-900">
                    {t("device.title")}
                  </h1>
                  <p className="mt-2 text-gray-600">
                    {t("device.description")}
                  </p>
                </div>

                <div className="space-y-2 text-left">
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
                    className="bg-white text-center text-2xl tracking-[0.3em] text-gray-900"
                    autoFocus
                    autoComplete="off"
                    disabled={phase === "verifying"}
                  />
                </div>

                {phase === "verifying" && (
                  <p className="text-sm text-gray-600">
                    {t("device.verifying")}
                  </p>
                )}

                {phase === "error" && error && (
                  <Alert
                    variant="destructive"
                    className="border-red-200 bg-red-50 text-left"
                  >
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
                <DeviceBadge label={t("device.badge")} />
                <div>
                  <h1 className="text-3xl font-bold text-gray-900">
                    {t("device.consentTitle")}
                  </h1>
                  <p className="mt-2 text-gray-600">
                    {t("device.consentDescription")}
                  </p>
                </div>

                <p className="text-lg font-medium text-gray-900">
                  {deviceInfo.client_name}
                </p>

                <div className="space-y-2">
                  <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">
                    {t("device.permissionsLabel")}
                  </p>
                  {renderScopeBadges(deviceInfo.scope)}
                </div>

                <div className="space-y-2">
                  <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">
                    {t("device.identityShareLabel")}
                  </p>
                  <div className="flex flex-wrap gap-2 justify-center">
                    <Badge variant="secondary">
                      {t("device.identityEmail")}
                    </Badge>
                    <Badge variant="secondary">
                      {t("device.identityWorkspace")}
                    </Badge>
                  </div>
                </div>

                <div className="flex gap-3 justify-center pt-2">
                  <Button
                    variant="outline"
                    onClick={() => handleConfirm(false)}
                    disabled={phase === "submitting"}
                    className="h-12 flex-1"
                  >
                    {phase === "submitting"
                      ? t("device.denying")
                      : t("device.deny")}
                  </Button>
                  <Button
                    onClick={() => handleConfirm(true)}
                    disabled={phase === "submitting"}
                    className="h-12 flex-1 rounded-full bg-kagura-accent text-base font-semibold text-white shadow-sm transition-colors hover:bg-[#a8380a] disabled:opacity-50"
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
                <Alert
                  variant="destructive"
                  className="border-red-200 bg-red-50 text-left"
                >
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
    </div>
  );
}

export default function DevicePage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center bg-white">
          <BrandSpinner />
        </div>
      }
    >
      <DevicePageInner />
    </Suspense>
  );
}
