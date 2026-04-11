/**
 * LoadingState Component
 *
 * Unified loading UI components for consistent UX.
 * Issue #31: Frontend Redesign Phase 5
 * Issue #58: Standardized Loading State Design
 */

import { cn, loading as loadingTokens } from "@/styles/design-tokens";

interface LoadingStateProps {
  lines?: number;
  className?: string;
}

// Deterministic width sequence (prevents SSR hydration mismatch).
// Values span 73-98% to match the previous Math.random()*30+70 distribution.
const SKELETON_WIDTHS = [95, 82, 91, 76, 88, 98, 73, 86, 94, 80] as const;

export function LoadingState({ lines = 3, className }: LoadingStateProps) {
  return (
    <div className={cn("space-y-3", className)}>
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          className="h-4 bg-slate-200 dark:bg-slate-800 rounded animate-pulse"
          style={{ width: `${SKELETON_WIDTHS[i % SKELETON_WIDTHS.length]}%` }}
        />
      ))}
    </div>
  );
}

export function CardLoadingState({ count = 3 }: { count?: number }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="h-48 bg-slate-200 dark:bg-slate-800 rounded-lg animate-pulse"
        />
      ))}
    </div>
  );
}

export function TableLoadingState({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div
          key={i}
          className="h-12 bg-slate-200 dark:bg-slate-800 rounded animate-pulse"
        />
      ))}
    </div>
  );
}

// ============================================================================
// Issue #58: New Loading Variants
// ============================================================================

type SpinnerSize = "xs" | "sm" | "md" | "lg" | "xl";
type SpinnerVariant = "default" | "brand";

interface SpinnerLoadingProps {
  size?: SpinnerSize;
  variant?: SpinnerVariant;
  message?: string;
  className?: string;
}

/**
 * Centered spinner loading for full sections/pages
 * Usage: <SpinnerLoading size="lg" message="Loading data..." />
 */
export function SpinnerLoading({
  size = "md",
  variant = "brand",
  message,
  className,
}: SpinnerLoadingProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-3 py-8",
        className,
      )}
    >
      <div
        className={cn(
          "rounded-full",
          loadingTokens.spinner[size],
          loadingTokens.colors[variant],
          loadingTokens.animations.spin,
        )}
      />
      {message && (
        <p className="text-sm text-slate-500 dark:text-slate-400">{message}</p>
      )}
    </div>
  );
}

interface InlineSpinnerProps {
  size?: SpinnerSize;
  variant?: SpinnerVariant;
  className?: string;
}

/**
 * Inline spinner for buttons and text
 * Usage: <InlineSpinner size="sm" />
 */
export function InlineSpinner({
  size = "sm",
  variant = "default",
  className,
}: InlineSpinnerProps) {
  return (
    <div
      className={cn(
        "rounded-full inline-block",
        loadingTokens.spinner[size],
        loadingTokens.colors[variant],
        loadingTokens.animations.spin,
        className,
      )}
    />
  );
}

interface PageLoadingProps {
  message?: string;
  showLogo?: boolean;
}

/**
 * Full page loading overlay
 * Usage: <PageLoading message="Loading application..." />
 */
export function PageLoading({
  message = "Loading...",
  showLogo = false,
}: PageLoadingProps) {
  return (
    <div className="fixed inset-0 bg-slate-900/50 backdrop-blur-sm z-50 flex items-center justify-center">
      <div className="bg-white dark:bg-slate-900 rounded-lg p-8 shadow-xl">
        <div className="flex flex-col items-center gap-4">
          {showLogo && (
            <div className="text-brand-green-600 text-4xl font-bold">K</div>
          )}
          <div
            className={cn(
              "rounded-full",
              loadingTokens.spinner.xl,
              loadingTokens.colors.brand,
              loadingTokens.animations.spin,
            )}
          />
          <p className="text-sm text-slate-600 dark:text-slate-400">
            {message}
          </p>
        </div>
      </div>
    </div>
  );
}
