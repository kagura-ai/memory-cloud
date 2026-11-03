"use client";

/**
 * Save bar for a settings section with staged edits (#2159, #2595).
 *
 * Render it in the flow after the section's last card: while `open` it sticks
 * to the bottom of the viewport as the section is scrolled (not a fixed
 * overlay, which would cover a second section's bar) and stays last in the
 * focus order. An accent border, a primary tint over the opaque blur and a
 * raised shadow make it read as a status strip, not another card.
 *
 * Only the message is the live region, so the Save button's "saving" relabel
 * is not re-announced. The region stays mounted, empty and visually hidden,
 * while the bar is closed: screen readers do not reliably announce a live
 * region inserted together with its text, but they do announce text added to
 * an existing one.
 */

import type { ReactNode } from "react";
import { Circle, Loader2, Save } from "lucide-react";
import { Button } from "@/components/ui/button";

interface UnsavedChangesBarProps {
  /** Whether something is unsaved: shows the bar and fills the status. */
  open: boolean;
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
  /** Set on the visible bar only. */
  "data-testid"?: string;
}

export function UnsavedChangesBar({
  open,
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
  // The same element tree whether open or not, so the <p> live region keeps
  // its DOM node and the message arrives as a change inside it.
  return (
    <div
      data-testid={open ? testId : undefined}
      className={
        open
          ? "sticky bottom-0 z-10 overflow-hidden rounded-lg border border-primary/40 bg-background/95 shadow-lg backdrop-blur supports-[backdrop-filter]:bg-background/80 animate-in fade-in slide-in-from-bottom-2 duration-200 motion-reduce:animate-none"
          : "sr-only"
      }
    >
      <div
        className={
          open
            ? "flex flex-wrap items-center justify-between gap-2 bg-primary/5 px-4 py-3"
            : undefined
        }
      >
        <p
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 text-sm font-medium text-foreground"
        >
          {open && (
            <>
              <Circle
                aria-hidden="true"
                className="h-2.5 w-2.5 shrink-0 fill-current text-primary"
              />
              {message}
            </>
          )}
        </p>
        {open && (
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
        )}
      </div>
    </div>
  );
}
