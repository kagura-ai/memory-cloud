"use client";

/**
 * Delete Memory Dialog
 *
 * Confirmation dialog for deleting a memory. Uses the UUID-addressed
 * `forgetMemory` endpoint (Issue #433).
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { AlertTriangle } from "lucide-react";
import { forgetMemory } from "@/lib/api/memory";
import type { MemoryReference } from "@/lib/types/memory";

interface DeleteMemoryDialogProps {
  memory: MemoryReference;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
}

export function DeleteMemoryDialog({
  memory,
  open,
  onOpenChange,
  onSuccess,
}: DeleteMemoryDialogProps) {
  const t = useTranslations("contextDetail.deleteDialog");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    try {
      setLoading(true);
      setError(null);
      await forgetMemory(memory.memory_id);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : t("failed"));
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    if (!loading) {
      onOpenChange(false);
      setError(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-red-600 dark:text-red-400">
            <AlertTriangle className="h-5 w-5" />
            {t("title")}
          </DialogTitle>
          <DialogDescription>{t("description")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {/* Preview block: identify the memory by summary + UUID. */}
          <div className="p-4 bg-slate-50 dark:bg-slate-900 rounded-lg space-y-2">
            <div>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {t("summaryLabel")}
              </span>
              <p className="font-medium">{memory.summary}</p>
            </div>
            <div>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {t("idLabel")}
              </span>
              <p className="text-xs font-mono break-all">{memory.memory_id}</p>
            </div>
            <div>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {t("scopeLabel")}
              </span>
              <p className="text-sm">{memory.scope}</p>
            </div>
            {memory.content && (
              <div>
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {t("previewLabel")}
                </span>
                <p className="text-sm text-slate-600 dark:text-slate-300 truncate">
                  {memory.content.substring(0, 100)}
                  {memory.content.length > 100 && "..."}
                </p>
              </div>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={handleClose} disabled={loading}>
            {t("cancel")}
          </Button>
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={loading}
          >
            {loading ? t("deleting") : t("confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
