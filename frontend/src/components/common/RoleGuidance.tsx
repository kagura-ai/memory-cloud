"use client";

import { useTranslations } from "next-intl";
import { WorkspaceRole } from "@/lib/auth/rbac";
import { Alert, AlertDescription } from "@/components/ui/alert";

export function RoleGuidance({ required }: { required: Exclude<WorkspaceRole, WorkspaceRole.Viewer> | "systemAdmin" }) {
  const t = useTranslations("featureAccess");
  return <Alert><AlertDescription>{t(required)}</AlertDescription></Alert>;
}
