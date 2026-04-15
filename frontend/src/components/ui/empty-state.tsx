import { type ReactNode } from "react";
import { type LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

interface EmptyStateProps {
  icon?: LucideIcon;
  emoji?: string;
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  children?: ReactNode;
  /**
   * Compact variant for in-tab / in-card empty states. Halves the vertical
   * footprint and tones down the icon framing — large landing-page empties
   * (full-page Resources list, plan-gate) stay on the default. Opt-in to keep
   * the existing 400+ call sites unchanged.
   */
  compact?: boolean;
}

export function EmptyState({
  icon: Icon,
  emoji,
  title,
  description,
  actionLabel,
  onAction,
  children,
  compact = false,
}: EmptyStateProps) {
  const containerCls = compact
    ? "flex min-h-[200px] flex-col items-center justify-center rounded-xl border border-dashed border-gray-300 bg-gray-50/50 p-6 text-center dark:border-gray-700 dark:bg-gray-900/30"
    : "flex min-h-[400px] flex-col items-center justify-center rounded-2xl border-2 border-dashed border-gray-300 bg-gray-50/50 p-12 text-center dark:border-gray-700 dark:bg-gray-900/30";

  const iconWrapperCls = compact
    ? "mb-3 inline-flex rounded-full bg-gray-100 p-3 text-gray-400 dark:bg-gray-800 dark:text-gray-500"
    : "mb-6 inline-flex rounded-full bg-gradient-to-br from-gray-100 to-gray-200 p-6 text-gray-400 dark:from-gray-800 dark:to-gray-900 dark:text-gray-500";

  const iconSizeCls = compact ? "h-6 w-6" : "h-12 w-12";
  const titleCls = compact
    ? "mb-1 text-base font-semibold text-gray-900 dark:text-gray-100"
    : "mb-2 text-2xl font-bold text-gray-900 dark:text-gray-100";
  const descriptionCls = compact
    ? "mb-4 max-w-md text-sm text-gray-600 dark:text-gray-400"
    : "mb-6 max-w-md text-gray-600 dark:text-gray-400";

  return (
    <div className={containerCls}>
      {Icon ? (
        <div className={iconWrapperCls}>
          <Icon className={iconSizeCls} />
        </div>
      ) : emoji ? (
        <div
          className={
            compact ? "mb-3 text-3xl opacity-60" : "mb-6 text-6xl opacity-50"
          }
        >
          {emoji}
        </div>
      ) : null}

      <h3 className={titleCls}>{title}</h3>
      <p className={descriptionCls}>{description}</p>

      {actionLabel && onAction && (
        <Button
          onClick={onAction}
          size={compact ? "default" : "lg"}
          className="bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white hover:from-brand-green-700 hover:to-emerald-700"
        >
          {actionLabel}
        </Button>
      )}

      {children}
    </div>
  );
}
