"use client";

/** Set-a-password link landing page (#1678): /password/setup?token=… */

import { Suspense } from "react";

import { SpinnerLoading } from "@/components/common/LoadingState";
import { PasswordLinkPage } from "@/components/auth/PasswordLinkPage";

export default function SetupPasswordPage() {
  return (
    <Suspense fallback={<SpinnerLoading size="lg" />}>
      <PasswordLinkPage mode="setup" />
    </Suspense>
  );
}
