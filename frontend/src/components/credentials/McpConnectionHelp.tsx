"use client";

import { Settings2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { McpConnectionCard } from "./McpConnectionCard";

export function McpConnectionHelp() {
  const t = useTranslations("mcpConnection");
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" className="min-h-11 gap-2 border-blue-300 bg-blue-50 text-blue-900 hover:bg-blue-100 dark:border-blue-700 dark:bg-slate-800 dark:text-blue-100 dark:hover:bg-slate-700">
          <Settings2 aria-hidden="true" className="h-4 w-4" />
          {t("help.title")}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto bg-slate-50 dark:bg-slate-900">
        <DialogTitle className="sr-only">{t("help.title")}</DialogTitle>
        <DialogDescription className="sr-only">{t("help.description")}</DialogDescription>
        <McpConnectionCard variant="summary" title={t("help.title")} />
      </DialogContent>
    </Dialog>
  );
}
