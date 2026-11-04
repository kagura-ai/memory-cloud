"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useSystemFeatures } from "@/hooks/useSystemFeatures";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useWorkspaceObjectPresence } from "@/hooks/useWorkspaceObjectPresence";

export function WorkspaceSectionNavigation() {
  const path = usePathname();
  const params = useSearchParams();
  const t = useTranslations("sectionNavigation");
  const features = useSystemFeatures();
  const { currentWorkspace: ws } = useWorkspace();
  const integrations = ["/workspace/integrations", "/workspace/resources", "/workspace/secrets"].some(prefix => path === prefix || path.startsWith(prefix + "/"));
  const settings = path === "/workspace/settings" || path.startsWith("/workspace/settings/");
  const apiKeys = integrations && (path.endsWith("/external-keys") ||
    (path.endsWith("/credentials") && params.get("tab") !== "oauth-apps"));
  const existingKeys = useWorkspaceObjectPresence("externalKeys", ws?.id,
    apiKeys && features?.byok === false && ws?.current_user_role === "owner");
  if ((!integrations && !settings) || (settings && params.get("create") === "true")) return null;

  const links = integrations ? [
    { label: "overview", href: "/workspace/integrations", active: path === "/workspace/integrations" },
    { label: "connectors", href: "/workspace/integrations/connectors", active: path.includes("/integrations/connectors") },
    { label: "resources", href: "/workspace/resources", active: path === "/workspace/resources" || path.startsWith("/workspace/resources/") },
    { label: "apiKeys", href: "/workspace/integrations/credentials?tab=api-keys", active: apiKeys },
    { label: "oauthApps", href: "/workspace/integrations/credentials?tab=oauth-apps", active: path.endsWith("/credentials") && params.get("tab") === "oauth-apps" },
    { label: "secrets", href: "/workspace/secrets", active: path === "/workspace/secrets" || path.startsWith("/workspace/secrets/") },
  ] : [
    ...(features?.plan_page === true ? [{ label: "plan", href: "/workspace/settings/plan", active: path.endsWith("/plan") || path.endsWith("/billing") }] : []),
    { label: "workspaceSettings", href: "/workspace/settings/general", active: path.endsWith("/general") },
  ];
  const keys = [
    { label: "kmcKeys", href: "/workspace/integrations/credentials?tab=api-keys", active: path.endsWith("/credentials") },
    ...(features?.byok === true || existingKeys === true
      ? [{ label: "providerKeys", href: "/workspace/integrations/external-keys", active: path.endsWith("/external-keys") }]
      : []),
  ];
  return <div className="mb-4 space-y-3">
    <nav aria-label={t(integrations ? "integrations" : "settings")} className="flex flex-wrap gap-1 border-b pb-2">
      {links.map(link => <Link key={link.label} href={link.href} aria-current={link.active ? "page" : undefined}
        className={`rounded-md px-3 py-2 text-sm font-medium ${link.active ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent"}`}>
        {t(link.label)}
      </Link>)}
    </nav>
    {apiKeys && <nav aria-label={t("keyKinds")} className="flex flex-wrap gap-1">
      {keys.map(link => <Link key={link.label} href={link.href} aria-current={link.active ? "page" : undefined}
        className={`rounded-md border px-3 py-2 text-sm ${link.active ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:bg-accent"}`}>
        {t(link.label)}
      </Link>)}
    </nav>}
  </div>;
}
