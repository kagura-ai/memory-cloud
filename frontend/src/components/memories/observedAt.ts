import { formatDateTime } from "@/lib/utils/datetime";

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Render a memory's `observed_at` (#2330): when the fact held.
 *
 * A date-only value is a calendar day, not an instant: it is shown as stored,
 * never through a timezone conversion (midnight UTC would otherwise read as
 * the previous or next day). A UTC datetime is shown like the other
 * timestamps, in the user's timezone.
 */
export function formatObservedAt(
  value: string,
  timezone: string | undefined,
  locale: string,
): string {
  if (DATE_ONLY.test(value)) {
    return value;
  }
  return formatDateTime(value, timezone, locale);
}
