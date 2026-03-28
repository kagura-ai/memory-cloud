/**
 * Quota Warning Component
 *
 * Issue #149: Plan tier enforcement
 *
 * Displays warning when approaching or exceeding quota limits.
 */

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { AlertCircle, AlertTriangle, XCircle } from 'lucide-react';
import { cn } from '@/styles/design-tokens';

interface QuotaWarningProps {
  current: number;
  limit: number;
  label: string;
  unit?: string;
  onUpgrade?: () => void;
  className?: string;
}

/**
 * Quota warning alert with progress bar.
 *
 * Displays:
 * - Nothing if usage < 80%
 * - Warning (yellow) if 80% <= usage < 95%
 * - Critical (red) if usage >= 95%
 * - Exceeded (destructive) if usage >= 100%
 *
 * @param current - Current usage
 * @param limit - Quota limit
 * @param label - Resource label (e.g., "Memories", "Storage")
 * @param unit - Unit label (e.g., "MB", "calls")
 * @param onUpgrade - Callback for upgrade button
 */
export function QuotaWarning({
  current,
  limit,
  label,
  unit = '',
  onUpgrade,
  className,
}: QuotaWarningProps) {
  const percentage = limit > 0 ? (current / limit) * 100 : 0;

  // Don't show warning if below 80%
  if (percentage < 80) {
    return null;
  }

  // Determine severity
  const isExceeded = percentage >= 100;
  const isCritical = percentage >= 95;
  const isWarning = percentage >= 80;

  const variant = isExceeded || isCritical ? 'destructive' : 'default';

  const Icon = isExceeded ? XCircle : isCritical ? AlertTriangle : AlertCircle;

  const title = isExceeded
    ? 'Quota Exceeded'
    : isCritical
    ? 'Critical: Approaching Limit'
    : 'Warning: Quota Usage High';

  const formatNumber = (num: number) => {
    return num.toLocaleString();
  };

  return (
    <Alert variant={variant} className={cn('mb-4', className)}>
      <Icon className="h-4 w-4" />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription>
        <div className="mt-2 space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="font-medium">
              {label}: {formatNumber(current)} / {formatNumber(limit)} {unit}
            </span>
            <span className="font-bold">{percentage.toFixed(1)}%</span>
          </div>

          <Progress
            value={Math.min(percentage, 100)}
            className={cn(
              'h-2',
              isExceeded || isCritical
                ? '[&>div]:bg-red-500'
                : '[&>div]:bg-yellow-500'
            )}
          />

          {isExceeded && (
            <p className="text-sm font-medium mt-2">
              You have exceeded your quota limit. Please delete some {label.toLowerCase()} or upgrade your plan.
            </p>
          )}

          {isCritical && !isExceeded && (
            <p className="text-sm mt-2">
              You are very close to your {label.toLowerCase()} limit. Consider upgrading to avoid service interruption.
            </p>
          )}

          {onUpgrade && (percentage >= 95 || isExceeded) && (
            <Button
              onClick={onUpgrade}
              size="sm"
              variant={isExceeded ? 'destructive' : 'default'}
              className="mt-2"
            >
              Upgrade Plan
            </Button>
          )}
        </div>
      </AlertDescription>
    </Alert>
  );
}
