"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Link2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { copyText } from "@/lib/utils/clipboard";

const COPIED_FEEDBACK_MS = 2000;

/**
 * The absolute permalink to a context's dashboard view. Built from the
 * browser's origin at click time; it carries only the context id.
 */
export function contextPermalink(contextId: string): string {
  return `${window.location.origin}/workspace/dashboard?context=${encodeURIComponent(contextId)}`;
}

interface CopyContextLinkButtonProps {
  contextId: string;
  contextName: string;
  className?: string;
}

/**
 * Icon button that copies the dashboard permalink of one context (#2936).
 * The accessible name includes the context's name; the same name is the
 * hover tooltip. A failed copy shows the workspace clipboard error message.
 */
export function CopyContextLinkButton({
  contextId,
  contextName,
  className,
}: CopyContextLinkButtonProps) {
  const t = useTranslations("workspace");
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const label = t("copyContextLink", { name: contextName });

  const handleCopy = async () => {
    try {
      // copyText degrades to an execCommand fallback before throwing (#987).
      await copyText(contextPermalink(contextId));
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), COPIED_FEEDBACK_MS);
      toast({ description: t("contextLinkCopied") });
    } catch (error) {
      console.error("Failed to copy context link:", error);
      toast({
        title: tCommon("error"),
        description: t("copyFailed"),
        variant: "destructive",
      });
    }
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className={className}
      onClick={() => void handleCopy()}
      aria-label={label}
      title={label}
    >
      {copied ? (
        <Check className="h-4 w-4 text-green-600" aria-hidden="true" />
      ) : (
        <Link2 className="h-4 w-4" aria-hidden="true" />
      )}
    </Button>
  );
}
