"use client";

import { useEffect, type ReactNode } from "react";
import { RoleGuidance } from "@/components/common/RoleGuidance";
import { usePathname } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { usePlanTierMatrix } from "@/hooks/usePlanFeatures";
import { matrixRestriction, withOwnerGuidance } from "@/lib/gates/featureGates";
import { isSystemInfoUnavailable, useSystemInfo } from "@/hooks/useSystemFeatures";
import { hasRole, hasWorkspaceRole, Role } from "@/lib/auth/rbac";
import { navigationForPath, type NavItem } from "@/lib/navigation/featureNavigation";
import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { PageContainer } from "@/components/common/PageContainer";
import { WorkspacePageHeader as PageHeader } from "@/components/dashboard/WorkspacePageHeader";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

function PlanHint({ feature }: { feature: NonNullable<NavItem["planHint"]> }) {
  const { currentWorkspace: ws } = useWorkspace();
  const info = useSystemInfo();
  const gate = matrixRestriction(feature, usePlanTierMatrix(), ws?.plan_name, useLocale(), false);
  if (!gate || gate.state !== "plan") return null;
  return <FeatureGateNotice gate={withOwnerGuidance(gate, ws?.current_user_role, info?.features?.plan_page === true)}
    scope={feature === "resources" || feature === "connectors" ? "create" : "all"} />;
}

// Generic feature titles only, including dynamic /:id routes. Never ask the
// protected child for a title, count, or existence hint before authorization.
function Explanation({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  useEffect(() => { document.title = title + " - Kagura Memory Cloud"; }, [title]);
  return <PageContainer><PageHeader title={title} description={description} />{children}</PageContainer>;
}

function RestrictedRoute({ item, system, children }: {
  item?: NavItem; system: boolean; children: ReactNode;
}) {
  const nav = useTranslations("sidebar");
  const t = useTranslations("featureAccess");
  const common = useTranslations("common");
  const { user } = useAuth();
  const { currentWorkspace: ws, loading } = useWorkspace();
  const info = useSystemInfo();
  const required = item?.requiredWorkspaceRole;
  const flags = item?.requiredFeature ? [item.requiredFeature].flat() : [];
  const systemDenied = system && !hasRole(user, Role.ADMIN);
  if (systemDenied) return <Explanation title={t("accessDenied")}><RoleGuidance required="systemAdmin" /></Explanation>;
  const missingRole = required && !hasWorkspaceRole(ws?.current_user_role, required) ? required : null;
  if ((!system && (loading || !ws)) || (flags.length > 0 && !info)) {
    return <SpinnerLoading message={common("loading")} />;
  }

  const title = nav(item?.nameKey ?? "admin");
  const page = (body: ReactNode) => (
    <Explanation title={title} description={!system && ws ? t("workspace", { name: ws.name }) : undefined}>
      <p className="mb-4 text-muted-foreground">{t(system ? "about.admin" : `about.${item?.nameKey}`)}</p>
      {body}
    </Explanation>
  );
  if (flags.length && isSystemInfoUnavailable(info)) {
    return page(<Alert><AlertDescription>{t("lookupFailed")}{" "}
      <Button variant="outline" size="sm" onClick={() => window.location.reload()}>{t("retry")}</Button>
    </AlertDescription></Alert>);
  }
  const deploymentOff = flags.some(flag => !info?.features?.[flag]);
  if (deploymentOff) {
    return page(
      <Alert>
        <AlertDescription>
          {t(item?.nameKey === "externalKeys" ? "currentlyUnavailable" : "deployment")}
        </AlertDescription>
      </Alert>,
    );
  }
  if (missingRole) {
    return page(<div className="space-y-3">
      <RoleGuidance required={missingRole} />
      {item?.planHint && <PlanHint feature={item.planHint} />}
    </div>);
  }
  return <>{children}</>;
}

export function FeatureRouteBoundary({ children }: { children: ReactNode }) {
  const path = usePathname();
  const system = path === "/admin" || path.startsWith("/admin/");
  const item = navigationForPath(path);
  if (!system && !item?.requiredWorkspaceRole && !item?.requiredFeature) return <>{children}</>;
  return <RestrictedRoute system={system} item={item}>{children}</RestrictedRoute>;
}
