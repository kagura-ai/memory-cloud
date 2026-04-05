/**
 * DateTime Utilities with Timezone Support
 *
 * Issue #175: User timezone settings for localized time display
 * Issue #223: i18n support
 */

import { formatDistanceToNow } from "date-fns";
import { ja } from "date-fns/locale";

/**
 * Format UTC datetime to user's timezone
 *
 * @param utcTime - ISO datetime string (UTC)
 * @param timezone - IANA timezone (e.g., 'Asia/Tokyo', 'America/New_York')
 * @param options - Intl.DateTimeFormat options
 * @returns Formatted datetime string
 *
 * @example
 * formatDateTime('2025-12-09T17:15:30', 'Asia/Tokyo')
 * // => '2025/12/10 2:15:30'
 */
export function formatDateTime(
  utcTime: string,
  timezone: string = "UTC",
  locale: string = "ja",
  options?: Intl.DateTimeFormatOptions,
): string {
  const date = new Date(utcTime);
  const bcp47 = locale === "ja" ? "ja-JP" : "en-US";

  const defaultOptions: Intl.DateTimeFormatOptions = {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: locale !== "ja",
  };

  return new Intl.DateTimeFormat(bcp47, {
    ...defaultOptions,
    ...options,
  }).format(date);
}

/**
 * Format UTC datetime to user's timezone (date only)
 *
 * @param utcTime - ISO datetime string (UTC)
 * @param timezone - IANA timezone
 * @returns Formatted date string
 *
 * @example
 * formatDate('2025-12-09T17:15:30', 'Asia/Tokyo')
 * // => '2025/12/10'
 */
export function formatDate(
  utcTime: string,
  timezone: string = "UTC",
  locale: string = "ja",
): string {
  const bcp47 = locale === "ja" ? "ja-JP" : "en-US";

  // Date-only strings (YYYY-MM-DD): display as-is without timezone conversion
  // Backend timeline API returns date-only strings already computed in UTC
  if (/^\d{4}-\d{2}-\d{2}$/.test(utcTime)) {
    const [year, month, day] = utcTime.split("-").map(Number);
    const date = new Date(year, month - 1, day);
    return new Intl.DateTimeFormat(bcp47, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(date);
  }

  const date = new Date(utcTime);
  return new Intl.DateTimeFormat(bcp47, {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

/**
 * Format UTC datetime to user's timezone (time only)
 *
 * @param utcTime - ISO datetime string (UTC)
 * @param timezone - IANA timezone
 * @returns Formatted time string
 *
 * @example
 * formatTime('2025-12-09T17:15:30', 'Asia/Tokyo')
 * // => '02:15:30'
 */
export function formatTime(
  utcTime: string,
  timezone: string = "UTC",
  locale: string = "ja",
): string {
  const date = new Date(utcTime);
  const bcp47 = locale === "ja" ? "ja-JP" : "en-US";

  return new Intl.DateTimeFormat(bcp47, {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: locale !== "ja",
  }).format(date);
}

/**
 * Format relative time with timezone awareness
 *
 * @param utcTime - ISO datetime string (UTC)
 * @param timezone - IANA timezone
 * @returns Relative time string (e.g., '5 minutes ago', 'about 3 hours')
 *
 * @example
 * formatRelativeTime('2025-12-09T17:15:30', 'Asia/Tokyo')
 * // => 'about 9 minutes ago'
 * formatRelativeTime('2025-12-09T17:15:30', 'Asia/Tokyo', 'en', false)
 * // => 'about 9 minutes'  (no suffix, for embedding in other strings)
 */
export function formatRelativeTime(
  utcTime: string,
  timezone: string = "UTC",
  locale: string = "en",
  addSuffix: boolean = true,
): string {
  const date = new Date(utcTime);
  const localeObj = locale === "ja" ? ja : undefined;

  return formatDistanceToNow(date, {
    addSuffix,
    locale: localeObj,
  });
}

/**
 * Common IANA timezones
 */
export const COMMON_TIMEZONES = [
  { value: "UTC", label: "UTC (Coordinated Universal Time)" },
  { value: "Asia/Tokyo", label: "Asia/Tokyo (Japan, JST)" },
  { value: "America/New_York", label: "America/New_York (US Eastern)" },
  { value: "America/Los_Angeles", label: "America/Los_Angeles (US Pacific)" },
  { value: "America/Chicago", label: "America/Chicago (US Central)" },
  { value: "Europe/London", label: "Europe/London (UK)" },
  { value: "Europe/Paris", label: "Europe/Paris (Central Europe)" },
  { value: "Asia/Shanghai", label: "Asia/Shanghai (China)" },
  { value: "Asia/Singapore", label: "Asia/Singapore" },
  { value: "Australia/Sydney", label: "Australia/Sydney" },
] as const;
