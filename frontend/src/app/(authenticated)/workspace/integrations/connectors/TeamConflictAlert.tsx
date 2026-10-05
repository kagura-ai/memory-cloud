"use client";

/**
 * #1753: the "this Slack team is already connected" refusal, told in the
 * reader's language.
 *
 * The create endpoint answers `409` `RES-002` with a stable `details.reason`:
 * `connector_team_connected_here` names the caller's own connector (id and
 * display name), `connector_team_connected_elsewhere` says nothing about the
 * other workspace. This module reads that reason — never the server's English
 * message — and renders the matching copy.
 *
 * #1880: what the "here" copy may say depends on where the request came from
 * and on what the server confirms. An OAuth sign-in refreshes the existing
 * connector's Slack permissions (the server stores the new install's bot token
 * on it and answers `details.token_refreshed: true`); only then does the copy
 * say so — an OAuth conflict without that flag gets the neutral copy. The
 * manual bind made no sign-in and its pasted token is dropped by the 409,
 * which its variant states. Without a source the copy is neutral.
 */

import { useCallback } from "react";
import { useTranslations } from "next-intl";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/base";

export type TeamConflict =
  | {
      kind: "here";
      connectorId: string;
      displayName: string | null;
      /** The server stored this sign-in's bot token on that connector. */
      tokenRefreshed: boolean;
    }
  | { kind: "elsewhere" };

/** Where the refused create came from; picks the "here" copy (#1880). */
export type TeamConflictSource = "oauth" | "manual";

// One whole sentence per case — never the connector id: `Resource.name` is
// nullable, and a UUID in the copy tells the reader nothing.
const HERE_KEYS = {
  neutral: { named: "here", unnamed: "hereUnnamed" },
  oauth: { named: "hereOAuth", unnamed: "hereOAuthUnnamed" },
  manual: { named: "hereManual", unnamed: "hereManualUnnamed" },
} as const;

/** The team conflict an error carries, or `null` for any other error. */
export function teamConflictOf(err: unknown): TeamConflict | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null;
  const details = err.details;
  if (!details || typeof details !== "object") return null;
  if (details.reason === "connector_team_connected_elsewhere") {
    return { kind: "elsewhere" };
  }
  if (
    details.reason === "connector_team_connected_here" &&
    typeof details.connector_id === "string"
  ) {
    return {
      kind: "here",
      connectorId: details.connector_id,
      displayName:
        typeof details.display_name === "string" && details.display_name
          ? details.display_name
          : null,
      tokenRefreshed: details.token_refreshed === true,
    };
  }
  return null;
}

/**
 * Formats a team conflict as one sentence. `teamName` is the Slack workspace
 * name when the install told us (OAuth); without it the copy says "this Slack
 * workspace" — never the team id. `source` selects the "here" variant; a
 * connector without a display name gets the copy that names none.
 */
export function useTeamConflictMessage(): (
  conflict: TeamConflict,
  teamName?: string | null,
  source?: TeamConflictSource,
) => string {
  const t = useTranslations("connectors.teamConflict");
  return useCallback(
    (
      conflict: TeamConflict,
      teamName?: string | null,
      source?: TeamConflictSource,
    ) => {
      const subject = teamName
        ? t("subjectNamed", { name: teamName })
        : t("subjectUnnamed");
      if (conflict.kind === "elsewhere") return t("elsewhere", { subject });
      // The refresh sentence needs the server's confirmation, not just an
      // OAuth origin: storing the token is best-effort on the server.
      const variant =
        source === "oauth" && !conflict.tokenRefreshed ? undefined : source;
      const keys = HERE_KEYS[variant ?? "neutral"];
      return conflict.displayName
        ? t(keys.named, { subject, connector: conflict.displayName })
        : t(keys.unnamed, { subject });
    },
    [t],
  );
}

export function TeamConflictAlert({
  conflict,
  teamName,
  source,
  onEditExisting,
}: {
  conflict: TeamConflict;
  teamName?: string | null;
  /** Where the refused create came from; omitted renders the neutral copy. */
  source?: TeamConflictSource;
  /** Opens the existing connector's editor; omitted when it cannot. */
  onEditExisting?: () => void;
}) {
  const t = useTranslations("connectors.teamConflict");
  const message = useTeamConflictMessage();
  return (
    <Alert variant="destructive">
      <AlertDescription className="space-y-2">
        <p>{message(conflict, teamName, source)}</p>
        {conflict.kind === "here" && onEditExisting && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onEditExisting}
          >
            {t("editExisting")}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
