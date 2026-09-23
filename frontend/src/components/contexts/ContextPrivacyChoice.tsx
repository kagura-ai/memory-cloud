"use client";

/**
 * ContextPrivacyChoice (#1646) — the Private / Shared radio pair both create
 * dialogs on the contexts page offer.
 *
 * It used to be two copies of the same markup that had drifted into two
 * copies of the COPY as well: the shared option named a tier ("Pro Plan" in
 * one dialog, "Pro" in the other) and linked to the Plan page under two more
 * strings. Now the markup lives here once, and the shared option's refusal is
 * the `shared_contexts` gate rendered by `FeatureGateNotice` (`control`): the
 * badge names the tier that includes shared contexts by its resolved label,
 * the line under it says what to do, and the upgrade CTA appears only where
 * the gate says this member can act on it.
 *
 * The private option is a ROLE rule (an admin may create shared contexts
 * only), not a plan gate, so it keeps its own copy — and the two dialogs
 * still word its helper line differently, which is the one thing `dialog`
 * selects.
 *
 * A pending gate (the tier matrix still resolving, or failing) leaves the
 * shared option inert and silent: no upsell before the answer is known.
 */

import { useId } from "react";
import { useTranslations } from "next-intl";

import { FeatureGateNotice } from "@/components/common/FeatureGateNotice";
import { Badge } from "@/components/ui/badge";
import { isBlocked, type FeatureGate } from "@/lib/gates/featureGates";
import { cn } from "@/lib/utils/cn";

/** The private option's helper line, per dialog and per role. */
const PRIVATE_HELP = {
  advanced: {
    admin: "onlyOwnersCanCreatePrivate",
    owner: "privateAvailableAllPlans",
  },
  quick: {
    admin: "adminsCanOnlyCreateShared",
    owner: "onlyYouCanAccess",
  },
} as const;

export interface ContextPrivacyChoiceProps {
  /** The selected option. */
  isPrivate: boolean;
  /** Called with the option the member picked. */
  onChange: (isPrivate: boolean) => void;
  /**
   * The member is a workspace admin (not the owner). Admins can only create
   * shared contexts, so the private option is inert for them.
   */
  isAdmin: boolean;
  /** `useFeatureGate("shared_contexts")`: may this tier create one? */
  shared: FeatureGate;
  /** Which create dialog this sits in; only the private helper differs. */
  dialog: keyof typeof PRIVATE_HELP;
}

export function ContextPrivacyChoice({
  isPrivate,
  onChange,
  isAdmin,
  shared,
  dialog,
}: ContextPrivacyChoiceProps) {
  const t = useTranslations("contexts");
  const sharedNameId = useId();
  const sharedGateId = useId();
  const sharedAllowed = shared.state === "allowed";
  const sharedRefused = isBlocked(shared);
  // A refusal may carry an upgrade CTA, and a button inside a <label> takes
  // the label's whole text as its accessible name. So the refused card is a
  // <div>; it has nothing to select anyway (its radio is disabled). The
  // radio is named by `aria-labelledby` in every state.
  const SharedCard = sharedRefused ? "div" : "label";

  const sharedName = (
    <span
      id={sharedNameId}
      className={cn("font-medium text-sm", !sharedAllowed && "opacity-60")}
    >
      <span aria-hidden="true">👥</span> {t("sharedOption")}
    </span>
  );

  return (
    <div className="space-y-2">
      <label
        className={cn(
          "flex items-start gap-3 p-3 border-2 rounded cursor-pointer",
          isPrivate
            ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
            : "border-gray-200 dark:border-gray-700",
          isAdmin && "opacity-60",
        )}
      >
        <input
          type="radio"
          value="private"
          checked={isPrivate}
          onChange={() => {
            if (!isAdmin) onChange(true);
          }}
          disabled={isAdmin}
          className="mt-1"
        />
        <div className="flex-1">
          <div className="font-medium text-sm flex items-center gap-2">
            <span aria-hidden="true">🔒</span> {t("privateOption")}
            {isAdmin && (
              <Badge
                variant="outline"
                className="ml-1 text-xs bg-gray-100 text-gray-700"
              >
                {t("ownerOnly")}
              </Badge>
            )}
          </div>
          <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
            {t(
              isAdmin ? PRIVATE_HELP[dialog].admin : PRIVATE_HELP[dialog].owner,
            )}
          </div>
        </div>
      </label>

      <SharedCard
        className={cn(
          "flex items-start gap-3 p-3 border-2 rounded",
          !isPrivate
            ? "border-purple-500 bg-purple-50 dark:bg-purple-900/20"
            : "border-gray-200 dark:border-gray-700",
          sharedAllowed ? "cursor-pointer" : "cursor-not-allowed",
        )}
      >
        <input
          type="radio"
          value="shared"
          checked={!isPrivate}
          onChange={() => {
            // Issue #270: only a tier with shared contexts can create one.
            if (sharedAllowed) onChange(false);
          }}
          disabled={!sharedAllowed}
          // The option's name alone; the refusal is its description.
          aria-labelledby={sharedNameId}
          aria-describedby={sharedRefused ? sharedGateId : undefined}
          className="mt-1"
        />
        <div className="flex-1">
          {sharedRefused ? (
            <FeatureGateNotice
              variant="control"
              gate={shared}
              id={sharedGateId}
              detail="description"
            >
              {sharedName}
            </FeatureGateNotice>
          ) : (
            <>
              <div>{sharedName}</div>
              {sharedAllowed && (
                <div className="text-xs text-gray-600 dark:text-gray-400 mt-1">
                  {t("teamMembersAccess")}
                </div>
              )}
            </>
          )}
        </div>
      </SharedCard>
    </div>
  );
}
