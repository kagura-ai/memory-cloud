/**
 * Error Banner Component
 *
 * Consistent error display across pages
 */

import { AlertTriangle } from 'lucide-react';

interface ErrorBannerProps {
  error: string | null;
  className?: string;
  /**
   * Pin the light red ramp regardless of the global theme. Use on always-light
   * surfaces: the auth pages (#1029) render a white card even when the app
   * theme is dark, so the theme-adaptive `dark:` variants below would paint
   * faint red-300 text on the white card. Default (false) keeps the
   * theme-adaptive behavior every other consumer relies on.
   */
  lightSurface?: boolean;
}

export function ErrorBanner({
  error,
  className = '',
  lightSurface = false,
}: ErrorBannerProps) {
  if (!error) return null;

  const container = lightSurface
    ? 'border-red-200 bg-red-50'
    : 'border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-900/20';
  const iconColor = lightSurface
    ? 'text-red-600'
    : 'text-red-600 dark:text-red-400';
  const textColor = lightSurface
    ? 'text-red-800'
    : 'text-red-800 dark:text-red-300';

  return (
    <div
      className={`rounded-lg border ${container} p-4 mb-6 ${className}`}
      role="alert"
      aria-live="assertive"
      aria-atomic="true"
    >
      <div className="flex items-center gap-2">
        <AlertTriangle
          className={`h-5 w-5 ${iconColor} flex-shrink-0`}
          aria-hidden="true"
        />
        <p className={`text-sm ${textColor}`}>{error}</p>
      </div>
    </div>
  );
}
