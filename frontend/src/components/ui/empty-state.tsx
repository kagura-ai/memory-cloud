import { type ReactNode } from 'react';
import { type LucideIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface EmptyStateProps {
  icon?: LucideIcon;
  emoji?: string;
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  children?: ReactNode;
}

export function EmptyState({
  icon: Icon,
  emoji,
  title,
  description,
  actionLabel,
  onAction,
  children,
}: EmptyStateProps) {
  return (
    <div className="flex min-h-[400px] flex-col items-center justify-center rounded-2xl border-2 border-dashed border-gray-300 bg-gray-50/50 p-12 text-center">
      {/* Icon or Emoji */}
      {Icon ? (
        <div className="mb-6 inline-flex rounded-full bg-gradient-to-br from-gray-100 to-gray-200 p-6 text-gray-400">
          <Icon className="h-12 w-12" />
        </div>
      ) : emoji ? (
        <div className="mb-6 text-6xl opacity-50">{emoji}</div>
      ) : null}

      {/* Title */}
      <h3 className="mb-2 text-2xl font-bold text-gray-900">{title}</h3>

      {/* Description */}
      <p className="mb-6 max-w-md text-gray-600">{description}</p>

      {/* Action Button */}
      {actionLabel && onAction && (
        <Button
          onClick={onAction}
          size="lg"
          className="bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white hover:from-brand-green-700 hover:to-emerald-700"
        >
          {actionLabel}
        </Button>
      )}

      {/* Custom children */}
      {children}
    </div>
  );
}
