"use client";

import { createContext, useContext, type ComponentProps, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { PageHeader } from "@/components/common/PageHeader";
import { WorkspaceSectionNavigation } from "./WorkspaceSectionNavigation";

const WorkspacePageContext = createContext<{ title?: string; hideDescription?: boolean; navigation: ReactNode }>({
  navigation: null,
});

export function WorkspacePageNavigationProvider({ children }: { children: ReactNode }) {
  const path = usePathname();
  const section = useTranslations("sectionNavigation");
  const workspace = useTranslations("workspace");
  const integrations = path === "/workspace/integrations"
    || path.startsWith("/workspace/integrations/")
    || path === "/workspace/resources"
    || path === "/workspace/secrets";
  const settings = path === "/workspace/settings" || path.startsWith("/workspace/settings/");
  const title = integrations
    ? section("integrations")
    : settings ? workspace("workspaceSettings") : undefined;

  return (
    <WorkspacePageContext.Provider value={{ title, hideDescription: integrations, navigation: <WorkspaceSectionNavigation /> }}>
      {children}
    </WorkspacePageContext.Provider>
  );
}

export function WorkspacePageHeader({ className, ...props }: ComponentProps<typeof PageHeader>) {
  const { title, hideDescription, navigation } = useContext(WorkspacePageContext);
  return (
    <div>
      <PageHeader {...props} title={title ?? props.title} description={hideDescription ? undefined : props.description} className={`!mb-3 ${className ?? ""}`} />
      {navigation}
    </div>
  );
}
