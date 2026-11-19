"use client";

/**
 * Export the contexts of an owned workspace, suspended or not (#2919): the
 * contexts the owner may export, one JSON download each. The caller keys it
 * by the workspace id, so each workspace starts from a fresh state.
 */

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Lock } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { LoadingState } from "@/components/common/LoadingState";
import { ExportContextButton } from "@/components/contexts/ExportContextButton";
import {
  listSlotWorkspaceContexts,
  type SlotWorkspace,
  type SlotWorkspaceContext,
} from "@/lib/api/workspaceSlots";

export function ExportWorkspaceDialog({
  workspace,
  onClose,
}: {
  /** The workspace to export; null = closed. */
  workspace: SlotWorkspace | null;
  onClose: () => void;
}) {
  const t = useTranslations("workspaceSlots");
  const [contexts, setContexts] = useState<SlotWorkspaceContext[] | null>(null);
  const [failed, setFailed] = useState(false);
  const workspaceId = workspace?.id;

  useEffect(() => {
    if (!workspaceId) return;
    let alive = true;
    listSlotWorkspaceContexts(workspaceId)
      .then((res) => {
        if (alive) setContexts(res.contexts);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [workspaceId]);

  return (
    <Dialog open={workspace !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {t("export.title", { name: workspace?.name ?? "" })}
          </DialogTitle>
          <DialogDescription>{t("export.description")}</DialogDescription>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">{t("export.privateNote")}</p>
        {failed ? (
          <Alert variant="destructive">
            <AlertDescription>{t("export.loadFailed")}</AlertDescription>
          </Alert>
        ) : contexts === null ? (
          <LoadingState lines={3} />
        ) : contexts.length === 0 ? (
          <EmptyState
            compact
            title={t("export.emptyTitle")}
            description={t("export.emptyDescription")}
          />
        ) : (
          <ul className="max-h-80 divide-y divide-slate-200 overflow-y-auto dark:divide-slate-700">
            {contexts.map((c) => (
              <li key={c.id} className="flex items-center justify-between gap-3 py-2">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="break-words">{c.name}</span>
                  {c.is_private && (
                    <Badge variant="outline" className="shrink-0">
                      <Lock className="mr-1 h-3 w-3" aria-hidden="true" />
                      {t("export.private")}
                    </Badge>
                  )}
                </span>
                <ExportContextButton contextId={c.id} contextName={c.name} />
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
