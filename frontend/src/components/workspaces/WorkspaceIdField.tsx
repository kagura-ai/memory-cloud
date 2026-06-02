"use client";

/**
 * WorkspaceIdField — read-only display of the workspace ID (UUID) with a
 * copy-to-clipboard button.
 *
 * Issue #873: users need the workspace_id for MCP/REST configuration, support
 * requests, cross-context references, and debugging, but it was not surfaced
 * anywhere in the UI. The ID is an identifier, NOT a secret, so it is shown in
 * plain text with no masking / Show-toggle (that model is reserved for
 * credentials — see useRevealableSecret). Copy routes through the shared
 * useCopyFeedback hook so its per-key timer cancels prior pending feedback
 * timers (avoids the timer-stomp / clipboard-clobber class of bug).
 */

import { useTranslations } from "next-intl";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useToast } from "@/hooks/use-toast";

interface WorkspaceIdFieldProps {
  workspaceId: string;
}

export function WorkspaceIdField({ workspaceId }: WorkspaceIdFieldProps) {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const { isCopied, copyToTarget } = useCopyFeedback();

  const handleCopy = async () => {
    try {
      await copyToTarget(workspaceId, "workspace-id");
      toast({
        title: tCommon("success"),
        description: t("workspaceIdCopied"),
      });
    } catch (err) {
      // useCopyFeedback re-throws clipboard errors so callers can surface
      // them — frontend rule: button-driven action failures use a toast.
      toast({
        variant: "destructive",
        title: tCommon("error"),
        description: err instanceof Error ? err.message : t("copyFailed"),
      });
    }
  };

  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
        {t("workspaceId")}
      </label>
      <div className="flex items-center gap-2">
        <code className="flex-1 px-3 py-2 text-sm bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100 rounded font-mono break-all">
          {workspaceId}
        </code>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void handleCopy()}
          className="h-9 shrink-0"
          title={t("copyWorkspaceId")}
          aria-label={t("copyWorkspaceId")}
        >
          {isCopied("workspace-id") ? (
            <Check className="h-4 w-4" />
          ) : (
            <Copy className="h-4 w-4" />
          )}
        </Button>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
        {t("workspaceIdHelp")}
      </p>
    </div>
  );
}
