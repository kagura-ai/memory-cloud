/**
 * Error Banner Component
 *
 * Consistent error display across pages
 */

import { AlertTriangle } from 'lucide-react';

interface ErrorBannerProps {
  error: string | null;
  className?: string;
}

export function ErrorBanner({ error, className = '' }: ErrorBannerProps) {
  if (!error) return null;

  return (
    <div
      className={`rounded-lg border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-900/20 p-4 mb-6 ${className}`}
      role="alert"
      aria-live="assertive"
      aria-atomic="true"
    >
      <div className="flex items-center gap-2">
        <AlertTriangle
          className="h-5 w-5 text-red-600 dark:text-red-400 flex-shrink-0"
          aria-hidden="true"
        />
        <p className="text-sm text-red-800 dark:text-red-300">{error}</p>
      </div>
    </div>
  );
}
