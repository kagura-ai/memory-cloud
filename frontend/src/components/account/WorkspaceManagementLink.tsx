"use client";

/**
 * The account settings entry to Workspace management (#2919). Rendered by
 * the profile page only while the deployment serves it
 * (`features.workspace_slots`).
 */

import Link from "next/link";
import { useTranslations } from "next-intl";
import { Building2 } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";

export function WorkspaceManagementLink() {
  const t = useTranslations("workspaceSlots");
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Building2 className="h-5 w-5" aria-hidden="true" />
          {t("profileLink.title")}
        </CardTitle>
        <CardDescription>{t("profileLink.description")}</CardDescription>
      </CardHeader>
      <CardContent>
        <Button asChild variant="outline">
          <Link href="/profile/workspaces">{t("profileLink.open")}</Link>
        </Button>
      </CardContent>
    </Card>
  );
}
