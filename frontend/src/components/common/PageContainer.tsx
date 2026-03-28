/**
 * PageContainer Component
 *
 * Unified page container with consistent spacing and layout.
 * Issue #31: Frontend Redesign Phase 5
 */

import { ReactNode } from 'react';
import { cn, spacing } from '@/styles/design-tokens';

interface PageContainerProps {
  children: ReactNode;
  className?: string;
}

export function PageContainer({ children, className }: PageContainerProps) {
  return (
    <div className={cn(spacing.page, spacing.section, className)}>
      {children}
    </div>
  );
}
