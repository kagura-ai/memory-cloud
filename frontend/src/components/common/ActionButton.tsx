/**
 * ActionButton Component
 *
 * Unified button component with consistent styling across the app.
 * Issue #31: Frontend Redesign Phase 5
 */

import { ReactNode, ButtonHTMLAttributes } from 'react';
import { cn, colors, transitions, borders } from '@/styles/design-tokens';
import { InlineSpinner } from '@/components/common/LoadingState';

type ButtonVariant = 'primary' | 'secondary' | 'outline' | 'ghost' | 'danger' | 'success';
type ButtonSize = 'sm' | 'md' | 'lg';

interface ActionButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  children: ReactNode;
  icon?: ReactNode;
  loading?: boolean;
  fullWidth?: boolean;
}

const sizeClasses: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-4 py-2 text-base',
  lg: 'px-6 py-3 text-lg',
};

export function ActionButton({
  variant = 'primary',
  size = 'md',
  children,
  icon,
  loading = false,
  fullWidth = false,
  className,
  disabled,
  ...props
}: ActionButtonProps) {
  const baseClasses = cn(
    'inline-flex items-center justify-center gap-2',
    'font-medium rounded-lg',
    'focus:outline-none focus:ring-2 focus:ring-offset-2',
    'disabled:opacity-50 disabled:cursor-not-allowed',
    transitions.default,
    sizeClasses[size],
    fullWidth && 'w-full'
  );

  const variantClass = colors.button[variant];

  return (
    <button
      className={cn(baseClasses, variantClass, className)}
      disabled={disabled || loading}
      {...props}
    >
      {loading && <InlineSpinner size="sm" />}
      {icon && !loading && icon}
      {children}
    </button>
  );
}
