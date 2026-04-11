"use client";

/**
 * MaskedSecretField
 *
 * Self-contained primitive for displaying a secret value with a default-masked
 * presentation, an explicit "Show" toggle, and a "Copy" button. Used for any
 * secret display where consistency with the credentials surface DX is desired
 * (API keys, OAuth client secrets, resource tokens, MCP config blocks).
 *
 * Behavior:
 * - When `value` is null (e.g. visibility window expired) the field renders
 *   `displayMask` only and disables both Show and Copy.
 * - When `value` is set, the visual content defaults to `displayMask`. The
 *   user must click Show to reveal the actual value.
 * - The Copy button always writes the actual `value` to the clipboard,
 *   regardless of whether the visual is masked. This is the security/DX
 *   compromise: the screen-share / shoulder-surf surface stays masked while
 *   the one-click DX is preserved.
 * - Copy triggers a 60s clipboard auto-clear by default (defense in depth)
 *   and a default-variant toast acknowledging the action.
 *
 * Renders nothing when `value === null` AND no fallback is desired — the
 * caller is responsible for showing a placeholder ("Key is hidden") in that
 * branch. The component itself shows the masked field unconditionally so
 * positioning stays stable across visibility transitions.
 */

import { useEffect } from "react";
import { Copy, Check, Eye, EyeOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { useRevealableSecret } from "@/hooks/useRevealableSecret";

export interface MaskedSecretFieldProps {
  /**
   * The plaintext secret to display and copy. `null` puts the field in the
   * disabled / placeholder state (visibility window expired or never opened).
   */
  value: string | null;
  /**
   * Optional prefix text rendered before the masked / revealed value.
   * Use cases: `"Bearer "`, `"sk_live_"`, etc. Not part of the masked area
   * (it stays visible regardless of reveal state). Default: empty string.
   */
  prefix?: string;
  /**
   * The mask string shown when the value is not revealed. Default uses
   * 12 bullet characters. Pass a longer or shorter string to match the
   * approximate visual length of the real secret without leaking the
   * actual length.
   */
  displayMask?: string;
  /**
   * Clipboard auto-clear duration in milliseconds. Default 60_000.
   * Set to 0 to disable the auto-clear (not recommended for live secrets).
   */
  autoClearMs?: number;
  /**
   * Toast title shown when the copy succeeds. Caller passes a translated
   * string so MaskedSecretField stays i18n-agnostic.
   */
  copyToastTitle: string;
  /**
   * Toast description shown when the copy succeeds. Should mention the
   * auto-clear behavior so the user knows clipboard contents are time-bound.
   */
  copyToastDescription: string;
  /**
   * Tooltip / aria-label for the Show button (revealed=false state).
   */
  showLabel: string;
  /**
   * Tooltip / aria-label for the Hide button (revealed=true state).
   */
  hideLabel: string;
  /**
   * Tooltip / aria-label for the Copy button.
   */
  copyLabel: string;
  /**
   * Optional className passed to the wrapping `<div>` for layout overrides.
   */
  className?: string;
  /**
   * Test id for the wrapping `<div>` to make assertions easy.
   */
  "data-testid"?: string;
}

const DEFAULT_MASK = "••••••••••••";

export function MaskedSecretField({
  value,
  prefix = "",
  displayMask = DEFAULT_MASK,
  autoClearMs,
  copyToastTitle,
  copyToastDescription,
  showLabel,
  hideLabel,
  copyLabel,
  className,
  "data-testid": testId,
}: MaskedSecretFieldProps) {
  const { toast } = useToast();
  const { revealed, toggle, copy, copied, hide } = useRevealableSecret({
    autoClearMs,
  });

  const disabled = value === null;

  // When the value transitions to null (visibility window expired or
  // backend hide), force-hide the revealed state so the secret is never
  // visible across that transition. This is the regression net the CSO
  // pre-review flagged as critical.
  useEffect(() => {
    if (value === null && revealed) {
      hide();
    }
  }, [value, revealed, hide]);

  const handleCopy = async () => {
    if (value === null) return;
    try {
      await copy(value);
      toast({
        title: copyToastTitle,
        description: copyToastDescription,
      });
    } catch (err) {
      // Re-throwing here would surface as an unhandled rejection because
      // this is an event handler. Surface as a generic error toast instead;
      // the consumer typically doesn't need to react to clipboard failures.
      toast({
        title: copyToastTitle,
        description: (err as Error).message,
        variant: "destructive",
      });
    }
  };

  const visibleValue = !disabled && revealed ? value : displayMask;

  return (
    <div
      className={`flex items-center gap-2 ${className ?? ""}`}
      data-testid={testId}
    >
      <code className="flex-1 bg-white dark:bg-gray-900 px-3 py-2 rounded border border-gray-300 dark:border-gray-600 text-sm font-mono break-all">
        {prefix}
        {visibleValue}
      </code>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={toggle}
        disabled={disabled}
        title={revealed ? hideLabel : showLabel}
        aria-label={revealed ? hideLabel : showLabel}
        aria-pressed={revealed}
      >
        {revealed ? (
          <EyeOff className="w-4 h-4" />
        ) : (
          <Eye className="w-4 h-4" />
        )}
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        onClick={handleCopy}
        disabled={disabled}
        title={copyLabel}
        aria-label={copyLabel}
      >
        {copied ? (
          <Check className="w-4 h-4 text-green-600" />
        ) : (
          <Copy className="w-4 h-4" />
        )}
      </Button>
    </div>
  );
}
