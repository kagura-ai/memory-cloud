/**
 * Service notices in AI tools (#2762): the person's own opt-in.
 *
 *   GET  /api/v1/me/mcp-notices   (session only)
 *   PUT  /api/v1/me/mcp-notices   (session only)
 *        { mcp_notices, wording_version } → 409 MCPNOTICE-005 when turning it
 *        on is not offered, 409 MCPNOTICE-006 when the wording changed, 429
 *        when changed too often. Turning it off always works.
 */

import { apiClient } from "./base";

export interface McpNoticesSetting {
  /** Whether the deployment offers notices in AI tools. */
  offered: boolean;
  /** The current answer (false when never answered). */
  mcp_notices: boolean;
  answered: boolean;
  source: "settings" | null;
  recorded_at: string | null;
  /** The wording version to send back when turning it on (null when not offered). */
  wording_version: string | null;
}

export interface McpNoticesUpdate extends McpNoticesSetting {
  recorded: boolean;
}

const SETTING_PATH = "/api/v1/me/mcp-notices";

export function getMcpNotices(): Promise<McpNoticesSetting> {
  return apiClient.get<McpNoticesSetting>(SETTING_PATH);
}

export function setMcpNotices(
  value: boolean,
  wordingVersion: string | null,
): Promise<McpNoticesUpdate> {
  return apiClient.put<McpNoticesUpdate>(SETTING_PATH, {
    mcp_notices: value,
    wording_version: value ? wordingVersion : null,
  });
}
