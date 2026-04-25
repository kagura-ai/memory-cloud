/**
 * Memory Detail Dialog
 *
 * Displays detailed information about a memory
 */

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Pencil, Trash2, Copy, Check } from "lucide-react";
import type { Memory } from "@/lib/types/memory";
import { formatDateTime } from "@/lib/utils/datetime";
import { useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useLocale, useTranslations } from "next-intl";

interface MemoryDetailDialogProps {
  memory: Memory;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Optional: omit to hide the Edit button entirely. #433 defers edit until a
  // UUID-addressed update endpoint lands; passing `undefined` keeps the dialog
  // honest (no ghost button).
  onEdit?: () => void;
  onDelete: () => void;
}

export function MemoryDetailDialog({
  memory,
  open,
  onOpenChange,
  onEdit,
  onDelete,
}: MemoryDetailDialogProps) {
  const { user } = useAuth();
  const locale = useLocale();
  const t = useTranslations("contextDetail.detailDialog");
  const [copied, setCopied] = useState(false);
  const [idCopied, setIdCopied] = useState(false);

  const copyValue = async () => {
    try {
      await navigator.clipboard.writeText(memory.value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      console.error("Failed to copy:", error);
    }
  };

  const copyId = async () => {
    try {
      await navigator.clipboard.writeText(memory.id);
      setIdCopied(true);
      setTimeout(() => setIdCopied(false), 2000);
    } catch (error) {
      console.error("Failed to copy:", error);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {memory.key}
            <Badge
              variant={memory.scope === "persistent" ? "default" : "outline"}
            >
              {memory.scope}
            </Badge>
          </DialogTitle>
          <DialogDescription>{t("memoryDetails")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          {/* Value */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium">{t("value")}</label>
              <Button
                variant="ghost"
                size="sm"
                onClick={copyValue}
                className="h-8"
              >
                {copied ? (
                  <>
                    <Check className="h-4 w-4 mr-1" />
                    {t("copied")}
                  </>
                ) : (
                  <>
                    <Copy className="h-4 w-4 mr-1" />
                    {t("copy")}
                  </>
                )}
              </Button>
            </div>
            <div className="p-3 bg-slate-50 dark:bg-slate-900 rounded-lg text-sm font-mono whitespace-pre-wrap break-words">
              {memory.value}
            </div>
          </div>

          <Separator />

          {/* Memory ID — full row with copy button */}
          <div>
            <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
              {t("memoryId")}
            </label>
            <div className="mt-1 flex items-center gap-2">
              <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded font-mono break-all">
                {memory.id}
              </code>
              <Button
                variant="ghost"
                size="sm"
                onClick={copyId}
                className="h-7 shrink-0"
                title={t("copyMemoryId")}
                aria-label={t("copyMemoryId")}
              >
                {idCopied ? (
                  <Check className="h-3.5 w-3.5" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </Button>
            </div>
          </div>

          {/* Metadata Grid — wrapper elements use <div> not <p> so the Badge
              children (which render as <div>) don't trigger the
              `<p> cannot contain <div>` HTML hydration warning. */}
          <div className="grid grid-cols-2 gap-4">
            {memory.type && (
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("type")}
                </label>
                <div className="mt-1">
                  <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded">
                    {memory.type}
                  </code>
                </div>
              </div>
            )}

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                {t("importance")}
              </label>
              <div className="mt-1">
                <Badge
                  variant={
                    memory.importance >= 0.8
                      ? "destructive"
                      : memory.importance >= 0.5
                        ? "default"
                        : "secondary"
                  }
                >
                  {memory.importance.toFixed(2)}
                </Badge>
              </div>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                {t("createdAt")}
              </label>
              <div className="mt-1 text-sm">
                {formatDateTime(memory.created_at, user?.timezone, locale)}
              </div>
            </div>

            <div>
              <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                {t("updatedAt")}
              </label>
              <div className="mt-1 text-sm">
                {formatDateTime(memory.updated_at, user?.timezone, locale)}
              </div>
            </div>
          </div>

          {/* Source — origin URI + type for memories imported from a vault,
              file, URL, etc. (Issue #215). Hidden if memory has no origin. */}
          {memory.source_uri && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("source")}
                </label>
                <div className="mt-1 flex items-center gap-2">
                  {memory.source_type && (
                    <Badge variant="outline" className="shrink-0">
                      {memory.source_type}
                    </Badge>
                  )}
                  <code className="text-xs bg-slate-100 dark:bg-slate-800 px-2 py-1 rounded break-all">
                    {memory.source_uri}
                  </code>
                </div>
              </div>
            </>
          )}

          {/* Tags */}
          {memory.tags && memory.tags.length > 0 && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("tags")}
                </label>
                <div className="flex flex-wrap gap-2 mt-2">
                  {memory.tags.map((tag) => (
                    <Badge key={tag} variant="outline">
                      {tag}
                    </Badge>
                  ))}
                </div>
              </div>
            </>
          )}

          {/* Metadata */}
          {memory.metadata && Object.keys(memory.metadata).length > 0 && (
            <>
              <Separator />
              <div>
                <label className="text-sm font-medium text-slate-500 dark:text-slate-400">
                  {t("metadata")}
                </label>
                <div className="mt-2 p-3 bg-slate-50 dark:bg-slate-900 rounded-lg text-sm font-mono">
                  <pre>{JSON.stringify(memory.metadata, null, 2)}</pre>
                </div>
              </div>
            </>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("close")}
          </Button>
          {onEdit && (
            <Button variant="outline" onClick={onEdit}>
              <Pencil className="h-4 w-4 mr-2" />
              {t("edit")}
            </Button>
          )}
          <Button variant="destructive" onClick={onDelete}>
            <Trash2 className="h-4 w-4 mr-2" />
            {t("delete")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
