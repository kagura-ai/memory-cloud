/**
 * Section Component
 *
 * Unified section component with optional title and description.
 * Issue #31: Frontend Redesign Phase 5
 */

import { ReactNode } from 'react';
import { cn, typography, spacing, layout } from '@/styles/design-tokens';

interface SectionProps {
  title?: string;
  description?: string;
  children: ReactNode;
  className?: string;
  headerActions?: ReactNode;
}

export function Section({
  title,
  description,
  children,
  className,
  headerActions,
}: SectionProps) {
  return (
    <section className={cn(spacing.sectionMargin, className)}>
      {(title || headerActions) && (
        <div className={cn(layout.flexBetween, 'mb-4')}>
          <div>
            {title && <h2 className={typography.h3}>{title}</h2>}
            {description && (
              <p className={cn(typography.description, 'mt-1')}>{description}</p>
            )}
          </div>
          {headerActions && <div>{headerActions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}
