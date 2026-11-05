"use client";

import Link from "next/link";
import { CircleHelp } from "lucide-react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export function ContextualHelp({ topic }: { topic: "credentials" | "search" }) {
  const t = useTranslations("gettingStarted");
  const title = t(`help.${topic}.title`);
  return (
    <Dialog>
      <TooltipProvider><Tooltip>
        <TooltipTrigger asChild><DialogTrigger asChild>
          <Button type="button" variant="ghost" size="sm" className="min-h-10 gap-1">
            <CircleHelp aria-hidden="true" className="h-4 w-4" />{title}
          </Button>
        </DialogTrigger></TooltipTrigger>
        <TooltipContent>{t(`help.${topic}.short`)}</TooltipContent>
      </Tooltip></TooltipProvider>
      <DialogContent>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{t(`help.${topic}.description`)}</DialogDescription>
        {topic === "credentials" && <Link href="/workspace/integrations" className="text-primary underline">{t("connections")}</Link>}
      </DialogContent>
    </Dialog>
  );
}
