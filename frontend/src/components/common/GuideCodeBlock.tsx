"use client";

import { useTranslations } from "next-intl";
import { CopyIconButton } from "@/components/common/CopyIconButton";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useToast } from "@/hooks/use-toast";

export function GuideCodeBlock({ text, label }: { text: string; label: string }) {
  const t = useTranslations("common");
  const { toast } = useToast();
  const { isCopied, copyToTarget } = useCopyFeedback();
  const copy = async (value: string, key: string) => {
    try {
      await copyToTarget(value, key);
    } catch {
      toast({ title: t("error"), description: t("copyFailedManualHint"), variant: "destructive" });
    }
  };
  return (
    <div className="relative rounded-lg border border-slate-200 bg-slate-50 p-3 pr-14 dark:border-slate-700 dark:bg-slate-900">
      <pre className="select-all whitespace-pre-wrap break-all font-mono text-xs leading-6 text-slate-900 dark:text-slate-100">{text}</pre>
      <div className="absolute right-2 top-2">
        <CopyIconButton value={text} copyKey="guide-code" label={label} isCopied={isCopied} onCopy={copy} />
      </div>
    </div>
  );
}
