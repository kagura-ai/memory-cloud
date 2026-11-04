"use client";

import { createContext, useContext, type ComponentProps, type ReactNode } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { WorkspaceSectionNavigation } from "./WorkspaceSectionNavigation";

const NavigationSlot = createContext<ReactNode>(null);

export function WorkspacePageNavigationProvider({ children }: { children: ReactNode }) {
  return <NavigationSlot.Provider value={<WorkspaceSectionNavigation />}>{children}</NavigationSlot.Provider>;
}

export function WorkspacePageHeader({ className, ...props }: ComponentProps<typeof PageHeader>) {
  const navigation = useContext(NavigationSlot);
  return <div>
    <PageHeader {...props} className={`!mb-3 ${className ?? ""}`} />
    {navigation}
  </div>;
}
