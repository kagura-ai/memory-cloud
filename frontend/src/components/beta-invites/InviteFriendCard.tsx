"use client";

/**
 * "Invite a friend" sidebar card (#1582).
 *
 * A nudge, not the only way in: the account menu keeps a permanent entry to
 * the same dialog, so this card can be dismissed for good.
 *
 * Shows only when the summary has loaded (the sidebar loads one only while
 * `features.beta_invites` is on), the user still has an invite to give
 * (`remaining === null` = system admin, unlimited), and × was never pressed.
 * Renders nothing until all of that is known — no flash-then-hide (#1571).
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { MailPlus, X } from "lucide-react";

import type { BetaInviteSummary } from "@/lib/api/beta-invites";
import { Button } from "@/components/ui/button";
import { cn, colors, transitions, typography } from "@/styles/design-tokens";

export const BETA_INVITE_CARD_DISMISS_KEY = "kagura:beta-invite-card-dismissed";

interface InviteFriendCardProps {
  /** `null` while pending, and always while the feature is off. */
  summary: BetaInviteSummary | null;
  onOpen: () => void;
}

export function InviteFriendCard({ summary, onOpen }: InviteFriendCardProps) {
  const t = useTranslations("betaInvites");
  // null = localStorage not read yet (SSR / first paint) → render nothing.
  const [dismissed, setDismissed] = useState<boolean | null>(null);

  useEffect(() => {
    try {
      setDismissed(
        window.localStorage.getItem(BETA_INVITE_CARD_DISMISS_KEY) === "true",
      );
    } catch {
      // Storage blocked (private mode): the card just cannot stay dismissed.
      setDismissed(false);
    }
  }, []);

  const dismiss = useCallback(() => {
    try {
      window.localStorage.setItem(BETA_INVITE_CARD_DISMISS_KEY, "true");
    } catch {
      // Hidden for this mount only.
    }
    setDismissed(true);
  }, []);

  if (summary === null || dismissed !== false) return null;
  if (summary.remaining !== null && summary.remaining <= 0) return null;

  return (
    <div className="px-4 pb-3">
      <div
        className={cn(
          "relative rounded-lg border",
          colors.border.default,
          colors.bg.hover,
          transitions.default,
        )}
      >
        {/* The card body is the button; × is a sibling, not a child, so the
            two controls never nest. */}
        <button
          type="button"
          onClick={onOpen}
          className="w-full rounded-lg p-3 pr-9 text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <span
            className={cn(
              "flex items-center gap-2 text-sm font-semibold",
              colors.text.primary,
            )}
          >
            <MailPlus
              className={cn("h-4 w-4 shrink-0", colors.text.accent)}
              aria-hidden="true"
            />
            {t("card.title")}
          </span>
          <span className={cn("mt-1 block", typography.caption)}>
            {t("card.body")}
          </span>
          {summary.remaining !== null && (
            <span className={cn("mt-1 block", typography.caption)}>
              {t("card.remaining", { remaining: summary.remaining })}
            </span>
          )}
        </button>
        <Button
          variant="ghost"
          size="icon"
          className="absolute right-1 top-1 h-7 w-7"
          onClick={dismiss}
          aria-label={t("card.dismiss")}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}

export default InviteFriendCard;
