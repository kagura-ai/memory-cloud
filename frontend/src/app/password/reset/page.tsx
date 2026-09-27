"use client";

/** Reset-password link landing page (#1678): /password/reset?token=… */

import { Suspense } from "react";

import { SpinnerLoading } from "@/components/common/LoadingState";
import { PasswordLinkPage } from "@/components/auth/PasswordLinkPage";

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<SpinnerLoading size="lg" />}>
      <PasswordLinkPage mode="reset" />
    </Suspense>
  );
}
