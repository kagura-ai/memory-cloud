/**
 * Announcement-email consent (#2761).
 *
 *   GET  /api/v1/me/announcement-emails            (session only)
 *   PUT  /api/v1/me/announcement-emails            (session only)
 *        { announcement_emails, wording_version } → 409 when turning it on is
 *        not offered or the wording changed, 429 when changed too often
 *   POST /api/v1/announcement-emails/unsubscribe   { token } (no auth)
 *        → always { status: "accepted" }; 429 after many invalid tokens
 */

import { apiClient } from "./base";

export interface AnnouncementEmailsSetting {
  /** Whether the deployment offers announcement emails. */
  offered: boolean;
  /** The current answer (false when never answered). */
  announcement_emails: boolean;
  answered: boolean;
  source: "signup" | "settings" | "unsubscribe_link" | null;
  recorded_at: string | null;
  /** The wording version to send back when turning it on (null when not offered). */
  wording_version: string | null;
}

export interface AnnouncementEmailsUpdate extends AnnouncementEmailsSetting {
  recorded: boolean;
}

const SETTING_PATH = "/api/v1/me/announcement-emails";

export function getAnnouncementEmails(): Promise<AnnouncementEmailsSetting> {
  return apiClient.get<AnnouncementEmailsSetting>(SETTING_PATH);
}

export function setAnnouncementEmails(
  value: boolean,
  wordingVersion: string | null,
): Promise<AnnouncementEmailsUpdate> {
  return apiClient.put<AnnouncementEmailsUpdate>(SETTING_PATH, {
    announcement_emails: value,
    wording_version: value ? wordingVersion : null,
  });
}

export function unsubscribeAnnouncementEmails(
  token: string,
): Promise<{ status: "accepted" }> {
  return apiClient.post<{ status: "accepted" }>(
    "/api/v1/announcement-emails/unsubscribe",
    { token },
  );
}
