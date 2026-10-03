"use client";

/**
 * CopyIconButton
 *
 * Ghost icon button that copies a value and flips its icon to a check while
 * the caller reports the value as "just copied". The feedback state lives in
 * the caller (`useCopyFeedback().isCopied`) so several buttons on one page
 * keep independent timers (#1836).
 */

import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";

export interface CopyIconButtonProps {
  /** The text written to the clipboard. */
  value: string;
  /** The key the caller's copy-feedback hook tracks this button under. */
  copyKey: string;
  /** Accessible name, also used as the tooltip. */
  label: string;
  isCopied: (key: string) => boolean;
  onCopy: (text: string, key: string) => void;
  className?: string;
}

export function CopyIconButton({
  value,
  copyKey,
  label,
  isCopied,
  onCopy,
  className,
}: CopyIconButtonProps) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onClick={() => onCopy(value, copyKey)}
      title={label}
      aria-label={label}
      className={className}
    >
      {isCopied(copyKey) ? (
        <Check className="w-4 h-4 text-green-600" />
      ) : (
        <Copy className="w-4 h-4" />
      )}
    </Button>
  );
}
