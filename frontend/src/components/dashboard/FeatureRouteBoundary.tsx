"use client";

import { useEffect, type ReactNode } from "react";
import { RoleGuidance } from "@/components/common/RoleGuidance";
import { usePathname, useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { usePlanTierMatrix } from "@/hooks/usePlanFeatures";
import { matrixRestriction, withOwnerGuidance } from "@/lib/gates/featureGates";
import { isSystemInfoUnavailable, useSystemInfo } from "@/hooks/useSystemFeatures";
import { hasWorkspaceRole, isSystemAdmin } from "@/lib/auth/rbac";
import { navigationForPath, type NavItem } from "@/lib/navigation/featureNavigation";
import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { SpinnerLoading } from "@/components/common/LoadingState";
import { PageContainer } from "@/components/common/PageContainer";
import { WorkspacePageHeader as PageHeader } from "@/components/dashboard/WorkspacePageHeader";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import NotFound from "@/app/not-found";

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

// #2767: `/admin` and `/admin/*` exist only where the deployment serves the
// system-admin surface to signed-in admins (`features.system_admin`) and the
// user is one. Everyone else gets the not-found page: no explanation, no
// admin title, and the protected page never mounts (so it makes no admin
// API call). A failed /system/info lookup gets the retry notice instead of a
// definitive not-found.
function SystemRoute({ children }: { children: ReactNode }) {
  const t = useTranslations("featureAccess");
  const common = useTranslations("common");
  const { user } = useAuth();
  const info = useSystemInfo();
  if (!info) return <SpinnerLoading message={common("loading")} />;
  if (isSystemInfoUnavailable(info)) {
    return <PageContainer><Alert><AlertDescription>{t("lookupFailed")}{" "}
      <Button variant="outline" size="sm" onClick={() => window.location.reload()}>{t("retry")}</Button>
    </AlertDescription></Alert></PageContainer>;
  }
  if (!isSystemAdmin(user, info.features)) return <NotFound />;
  return <>{children}</>;
}

function RestrictedRoute({ item, landing, children }: {
  item?: NavItem; landing: boolean; children: ReactNode;
}) {
  const router = useRouter();
  const nav = useTranslations("sidebar");
  const t = useTranslations("featureAccess");
  const common = useTranslations("common");
  const { currentWorkspace: ws, loading } = useWorkspace();
  const info = useSystemInfo();
  const required = item?.requiredWorkspaceRole;
  const flags = item?.requiredFeature ? [item.requiredFeature].flat() : [];
  const missingRole = required && !hasWorkspaceRole(ws?.current_user_role, required) ? required : null;
  // Landing here (sign-in, a link, a reload) is not a menu click: go where this
  // role has data. A menu click still gets the explanation below.
  const fallback = landing && !loading && ws && missingRole ? item?.landingFallback : undefined;
  useEffect(() => { if (fallback) router.replace(fallback); }, [fallback, router]);
  if (fallback || loading || !ws || (flags.length > 0 && !info)) {
    return <SpinnerLoading message={common("loading")} />;
  }

  const title = nav(item?.nameKey ?? "admin");
  const page = (body: ReactNode) => (
    <Explanation title={title} description={ws ? t("workspace", { name: ws.name }) : undefined}>
      <p className="mb-4 text-muted-foreground">{t(`about.${item?.nameKey}`)}</p>
      {body}
    </Explanation>
  );
  if (flags.length && isSystemInfoUnavailable(info)) {
    return page(<Alert><AlertDescription>{t("lookupFailed")}{" "}
      <Button variant="outline" size="sm" onClick={() => window.location.reload()}>{t("retry")}</Button>
    </AlertDescription></Alert>);
  }
  const deploymentOff = flags.some(flag => !info?.features?.[flag]);
  // With BYOK off an owner can still manage previously stored external keys;
  // its existing console enforces the provisioning-only deployment restriction.
  if (deploymentOff && !(item?.existingObjectsFallback && !missingRole)) {
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

/** `landing`: this route is the first one the signed-in app shows (see the layout). */
export function FeatureRouteBoundary({ children, landing = false }: { children: ReactNode; landing?: boolean }) {
  const path = usePathname();
  const system = path === "/admin" || path.startsWith("/admin/");
  const item = navigationForPath(path);
  if (system) return <SystemRoute>{children}</SystemRoute>;
  if (!item?.requiredWorkspaceRole && !item?.requiredFeature) return <>{children}</>;
  return <RestrictedRoute item={item} landing={landing}>{children}</RestrictedRoute>;
}
