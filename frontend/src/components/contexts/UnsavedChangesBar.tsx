"use client";

/**
 * Save bar for a settings section with staged edits (#2159, #2595).
 *
 * Render it only while something is unsaved, in the flow after the section's
 * last card: it sticks to the bottom of the viewport while the section is
 * scrolled (not a fixed overlay, which would cover a second section's bar)
 * and stays last in the focus order. An accent border, a primary tint over
 * the opaque blur and a raised shadow make it read as a status strip, not
 * another card. Only the message is the live region, so the Save button's
 * "saving" relabel is not re-announced.
 */

import type { ReactNode } from "react";
import { Circle, Loader2, Save } from "lucide-react";
import { Button } from "@/components/ui/button";

interface UnsavedChangesBarProps {
  /** What is unsaved, or why it cannot be saved; a polite status. */
  message: ReactNode;
  discardLabel: string;
  saveLabel: string;
  savingLabel: string;
  onDiscard: () => void;
  onSave: () => void;
  saving: boolean;
  discardDisabled?: boolean;
  saveDisabled?: boolean;
  "data-testid"?: string;
}

export function UnsavedChangesBar({
  message,
  discardLabel,
  saveLabel,
  savingLabel,
  onDiscard,
  onSave,
  saving,
  discardDisabled = false,
  saveDisabled = false,
  "data-testid": testId,
}: UnsavedChangesBarProps) {
  return (
    <div
      data-testid={testId}
      className="sticky bottom-0 z-10 overflow-hidden rounded-lg border border-primary/40 bg-background/95 shadow-lg backdrop-blur supports-[backdrop-filter]:bg-background/80 animate-in fade-in slide-in-from-bottom-2 duration-200 motion-reduce:animate-none"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 bg-primary/5 px-4 py-3">
        <p
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 text-sm font-medium text-foreground"
        >
          <Circle
            aria-hidden="true"
            className="h-2.5 w-2.5 shrink-0 fill-current text-primary"
          />
          {message}
        </p>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={onDiscard}
            disabled={discardDisabled}
          >
            {discardLabel}
          </Button>
          <Button size="sm" onClick={onSave} disabled={saveDisabled}>
            {saving ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Save className="h-4 w-4 mr-2" />
            )}
            {saving ? savingLabel : saveLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}
