/**
 * ContextMembersTabPanel
 *
 * The context page's Members tab (#2895), open to every workspace role:
 * - shared or public context: the member list (MembersSection), managed by
 *   workspace owners and admins and read-only, with a notice, for others;
 * - private context: a notice instead of a list, plus a button to the
 *   Settings tab (where sharing is changed) for owners and admins;
 * - always: how privacy and access decide who reaches the context
 *   (docs/concepts.md, "Workspace" and "Context").
 */

"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { Info, Lock, ShieldCheck } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { MembersSection } from "@/components/contexts/MembersSection";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { hasWorkspaceRole, WorkspaceRole } from "@/lib/auth/rbac";
import type { Context } from "@/lib/types/context";

interface ContextMembersTabPanelProps {
  contextId: string;
  context: Context;
  /** Switches the page to its Settings tab. */
  onOpenSettings: () => void;
}

export function ContextMembersTabPanel({
  contextId,
  context,
  onOpenSettings,
}: ContextMembersTabPanelProps) {
  const t = useTranslations("contextDetail");
  const { currentWorkspace } = useWorkspace();
  const canManage = hasWorkspaceRole(
    currentWorkspace?.current_user_role,
    WorkspaceRole.Admin,
  );

  return (
    <div className="space-y-6">
      {context.is_private ? (
        <Alert data-testid="members-private-notice">
          <Lock className="h-4 w-4" />
          <AlertTitle>{t("membersTab.privateTitle")}</AlertTitle>
          <AlertDescription>
            <p>{t("membersTab.privateNotice")}</p>
            {canManage && (
              <Button
                variant="outline"
                size="sm"
                className="mt-3"
                onClick={onOpenSettings}
              >
                {t("membersTab.openSettings")}
              </Button>
            )}
          </AlertDescription>
        </Alert>
      ) : (
        <>
          {!canManage && (
            <Alert data-testid="members-readonly-notice">
              <Info className="h-4 w-4" />
              <AlertDescription>
                {t("membersTab.readOnlyNotice")}
              </AlertDescription>
            </Alert>
          )}
          <MembersSection contextId={contextId} context={context} />
        </>
      )}

      <Card data-testid="members-access-explanation">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-5 w-5" />
            {t("membersTab.explanationTitle")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted-foreground">
          <ul className="list-disc space-y-2 pl-5">
            <li>{t("membersTab.explainPrivate")}</li>
            <li>{t("membersTab.explainShared")}</li>
            <li>{t("membersTab.explainPublic")}</li>
            <li>{t("membersTab.explainRoles")}</li>
            <li>{t("membersTab.explainAccessListAdd")}</li>
          </ul>
          <p>{t("membersTab.explainAccessListWhere")}</p>
          {canManage && (
            <Link
              href="/workspace/members"
              className="inline-block font-medium text-primary underline-offset-4 hover:underline"
            >
              {t("membersTab.workspaceMembersLink")}
            </Link>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
