"use client";

/**
 * Download a context as portable JSON (#950 export, surfaced by #1941).
 *
 * Export stays allowed while a workspace is over its Free capacity, so the
 * owner can keep a copy of a context before deleting it.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { exportContext } from "@/lib/api/contexts";

/** A file name safe on every OS, from the context's name. */
export function exportFileName(contextName: string): string {
  const slug = contextName
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${slug || "context"}-export.json`;
}

export function ExportContextButton({
  contextId,
  contextName,
}: {
  contextId: string;
  contextName: string;
}) {
  const t = useTranslations("contextDetail");
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);

  const onExport = async () => {
    setBusy(true);
    try {
      const data = await exportContext(contextId);
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = exportFileName(contextName);
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      toast({ title: t("exportFailed"), variant: "destructive" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => void onExport()}
      disabled={busy}
    >
      <Download className="mr-2 h-4 w-4" />
      {t("export")}
    </Button>
  );
}
