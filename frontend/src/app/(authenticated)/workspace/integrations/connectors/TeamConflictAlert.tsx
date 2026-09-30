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
 */

import { useCallback } from "react";
import { useTranslations } from "next-intl";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/base";

export type TeamConflict =
  | { kind: "here"; connectorId: string; displayName: string | null }
  | { kind: "elsewhere" };

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
    };
  }
  return null;
}

/**
 * Formats a team conflict as one sentence. `teamName` is the Slack workspace
 * name when the install told us (OAuth); without it the copy says "this Slack
 * workspace" — never the team id.
 */
export function useTeamConflictMessage(): (
  conflict: TeamConflict,
  teamName?: string | null,
) => string {
  const t = useTranslations("connectors.teamConflict");
  return useCallback(
    (conflict: TeamConflict, teamName?: string | null) => {
      const subject = teamName
        ? t("subjectNamed", { name: teamName })
        : t("subjectUnnamed");
      if (conflict.kind === "elsewhere") return t("elsewhere", { subject });
      return t("here", {
        subject,
        connector: conflict.displayName ?? conflict.connectorId,
      });
    },
    [t],
  );
}

export function TeamConflictAlert({
  conflict,
  teamName,
  onEditExisting,
}: {
  conflict: TeamConflict;
  teamName?: string | null;
  /** Opens the existing connector's editor; omitted when it cannot. */
  onEditExisting?: () => void;
}) {
  const t = useTranslations("connectors.teamConflict");
  const message = useTeamConflictMessage();
  return (
    <Alert variant="destructive">
      <AlertDescription className="space-y-2">
        <p>{message(conflict, teamName)}</p>
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
