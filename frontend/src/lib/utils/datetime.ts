/**
 * DateTime Utilities with Timezone Support
 *
 * Issue #175: User timezone settings for localized time display
 * Issue #223: i18n support
 */

import { formatDistanceToNow } from 'date-fns';
import { ja } from 'date-fns/locale';

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
  timezone: string = 'UTC',
  options?: Intl.DateTimeFormatOptions
): string {
  const date = new Date(utcTime);

  const defaultOptions: Intl.DateTimeFormatOptions = {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  };

  return new Intl.DateTimeFormat('ja-JP', {
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
  timezone: string = 'UTC'
): string {
  const date = new Date(utcTime);

  return new Intl.DateTimeFormat('ja-JP', {
    timeZone: timezone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
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
  timezone: string = 'UTC'
): string {
  const date = new Date(utcTime);

  return new Intl.DateTimeFormat('ja-JP', {
    timeZone: timezone,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date);
}

/**
 * Format relative time with timezone awareness
 *
 * @param utcTime - ISO datetime string (UTC)
 * @param timezone - IANA timezone
 * @returns Relative time string (e.g., '5 minutes ago', 'in 2 hours')
 *
 * @example
 * formatRelativeTime('2025-12-09T17:15:30', 'Asia/Tokyo')
 * // => 'about 9 minutes'
 */
export function formatRelativeTime(
  utcTime: string,
  timezone: string = 'UTC',
  locale: string = 'en'
): string {
  // Use date-fns with locale support (Issue #223)
  const date = new Date(utcTime);
  const localeObj = locale === 'ja' ? ja : undefined;

  return formatDistanceToNow(date, {
    addSuffix: false,  // We add suffix in the UI translation
    locale: localeObj,
  });
}

/**
 * Common IANA timezones
 */
export const COMMON_TIMEZONES = [
  { value: 'UTC', label: 'UTC (Coordinated Universal Time)' },
  { value: 'Asia/Tokyo', label: 'Asia/Tokyo (Japan, JST)' },
  { value: 'America/New_York', label: 'America/New_York (US Eastern)' },
  { value: 'America/Los_Angeles', label: 'America/Los_Angeles (US Pacific)' },
  { value: 'America/Chicago', label: 'America/Chicago (US Central)' },
  { value: 'Europe/London', label: 'Europe/London (UK)' },
  { value: 'Europe/Paris', label: 'Europe/Paris (Central Europe)' },
  { value: 'Asia/Shanghai', label: 'Asia/Shanghai (China)' },
  { value: 'Asia/Singapore', label: 'Asia/Singapore' },
  { value: 'Australia/Sydney', label: 'Australia/Sydney' },
] as const;
