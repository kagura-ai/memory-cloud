/**
 * Plan Badge Component
 *
 * Issue #149: Plan tier enforcement
 * Issue #350: Customizable display names (S/M/L default)
 *
 * Displays workspace plan tier with color coding.
 * Display names are configurable for SaaS forks.
 */

import { Badge } from '@/components/ui/badge';
import { cn } from '@/styles/design-tokens';

export type PlanTier = 'free' | 'basic' | 'pro';

interface PlanBadgeProps {
  planName: PlanTier;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

const PLAN_COLORS: Record<PlanTier, string> = {
  free: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-100',
  basic: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-100',
  pro: 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-100',
};

// Default display names (customizable via NEXT_PUBLIC_PLAN_*_DISPLAY_NAME env vars)
const PLAN_LABELS: Record<PlanTier, string> = {
  free: process.env.NEXT_PUBLIC_PLAN_FREE_DISPLAY_NAME || 'S',
  basic: process.env.NEXT_PUBLIC_PLAN_BASIC_DISPLAY_NAME || 'M',
  pro: process.env.NEXT_PUBLIC_PLAN_PRO_DISPLAY_NAME || 'L',
};

const SIZE_CLASSES = {
  sm: 'text-xs px-2 py-0.5',
  md: 'text-sm px-2.5 py-1',
  lg: 'text-base px-3 py-1.5',
};

export function PlanBadge({ planName, size = 'md', className }: PlanBadgeProps) {
  return (
    <Badge
      className={cn(
        PLAN_COLORS[planName],
        SIZE_CLASSES[size],
        'font-semibold',
        className
      )}
    >
      {PLAN_LABELS[planName]}
    </Badge>
  );
}
