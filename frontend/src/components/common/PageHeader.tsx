/**
 * PageHeader Component
 *
 * Unified page header component with title, description, and optional actions.
 * Issue #31: Frontend Redesign Phase 5
 */

import { ReactNode } from 'react';
import { cn, typography, spacing } from '@/styles/design-tokens';

interface PageHeaderProps {
  title: string | ReactNode;
  description?: string;
  actions?: ReactNode;
  className?: string;
}

export function PageHeader({
  title,
  description,
  actions,
  className,
}: PageHeaderProps) {
  return (
    <div className={cn('flex items-center justify-between', spacing.sectionMargin, className)}>
      <div>
        <h1 className={typography.h1}>{title}</h1>
        {description && (
          <p className={cn(typography.description, 'mt-2')}>{description}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-3">{actions}</div>}
    </div>
  );
}
